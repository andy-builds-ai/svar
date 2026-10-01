"""Add a workspace's key, or remove a workspace: its key, budget, documents and vectors.

No customer is named in the repository. A workspace exists in AWS: a key under the
stack output ``ApiKeyParameterPath``, documents under ``workspaces/<name>/`` in the
documents bucket. This script adds the first and removes all of it again.
"""

import argparse
import hashlib
import secrets
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from scripts import utf8_output
from scripts.aws import (
    REGION,
    STACK_NAME,
    AwsProblem,
    client,
    error_code,
    explained,
    read_stack_outputs,
    resolve,
)
from scripts.ingest import (
    BUCKET_PREFIX,
    MAX_WORKSPACE_CHARACTERS,
    WORKSPACE_NAME,
    delete_keys,
    format_job,
    job_succeeded,
    remote_keys,
    run_ingestion_job,
)

#: 32 random bytes, written as 43 URL-safe characters: as strong as the keys the
#: stack generated until 2026-10-01.
KEY_BYTES = 32

#: What the SSM console shows next to the key. It names the header the function
#: reads, ``X-Andur-Key``; CloudFront replaces ``Authorization`` on the way.
KEY_DESCRIPTION = "Andur workspace key. Send it in the header X-Andur-Key."

#: The partition key of the budget table: the SHA-256 of the key, in hex
#: (``BUDGET_KEY_ATTRIBUTE`` and ``key_hash`` in app/handler.py).
BUDGET_KEY_ATTRIBUTE = "key_hash"

ADD_OUTPUTS = ("ApiKeyParameterPath",)
REMOVE_OUTPUTS = (
    "ApiKeyParameterPath",
    "BudgetTableName",
    "DocumentsBucketName",
    "KnowledgeBaseId",
    "DataSourceId",
)


def name_problem(workspace: str) -> str | None:
    """Why ``workspace`` is not a name the answer function accepts, or None if it is."""
    if len(workspace) > MAX_WORKSPACE_CHARACTERS:
        return f"A workspace name is at most {MAX_WORKSPACE_CHARACTERS} characters."
    if not WORKSPACE_NAME.match(workspace):
        return "A workspace name is lowercase letters, digits and single hyphens."
    return None


def key_parameter(path: str, workspace: str) -> str:
    """``<path>/<workspace>``, for a path the way the stack writes it."""
    if not path.startswith("/") or path.endswith("/"):
        raise AwsProblem(f"The key path must start with a slash and not end with one: {path!r}.")
    return f"{path}/{workspace}"


def documents_prefix(workspace: str) -> str:
    """``workspaces/<workspace>/`` — with the slash, so ``kalv`` never reaches ``kalvenstett``."""
    return f"{BUCKET_PREFIX}{workspace}/"


def key_hash(key: str) -> str:
    """The budget table's key for ``key``, computed as the answer function computes it."""
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


# --- add -----------------------------------------------------------------


def new_key() -> str:
    """A fresh random key, safe to send in an HTTP header."""
    return secrets.token_urlsafe(KEY_BYTES)


def add_key(ssm: Any, name: str) -> str:
    """Store a new key under ``name`` and return it; an existing key is never replaced."""
    key = new_key()
    try:
        with explained(f"Storing the key {name}"):
            ssm.put_parameter(
                Name=name,
                Description=KEY_DESCRIPTION,
                Value=key,
                Type="SecureString",
                Tier="Standard",
                Overwrite=False,
            )
    except AwsProblem as problem:
        if error_code(problem.__cause__) != "ParameterAlreadyExists":
            raise
        # botocore repeats a call whose answer got lost; the first try may have stored this
        # very key. Only a different key is somebody else's.
        if read_key(ssm, name) != key:
            raise AwsProblem(
                f"There is a key {name} already. It stays as it is; nothing was changed."
            ) from problem.__cause__
    return key


# --- remove --------------------------------------------------------------


@dataclass
class Removal:
    """Everything that belongs to one workspace, found before anything is deleted."""

    workspace: str
    parameter: str
    key: str | None = field(default=None, repr=False)
    #: Budget items by key hash: the current key's, and every item the answer function
    #: wrote for this workspace, earlier keys' included.
    budgets: dict[str, dict[str, Any]] = field(default_factory=dict)
    documents: list[str] = field(default_factory=list)


def read_key(ssm: Any, name: str) -> str | None:
    """The key under ``name``, or None if there is none."""
    try:
        with explained(f"Reading the key {name}"):
            response = ssm.get_parameter(Name=name, WithDecryption=True)
    except AwsProblem as problem:
        if error_code(problem.__cause__) == "ParameterNotFound":
            return None
        raise
    return response["Parameter"]["Value"]


def budget_key(key: str) -> dict[str, dict[str, str]]:
    return {BUDGET_KEY_ATTRIBUTE: {"S": key_hash(key)}}


def read_budget(dynamodb: Any, table: str, key: str) -> dict[str, Any] | None:
    """The budget item of ``key``, or None if the key has not asked yet."""
    with explained(f"Reading the budget of the key in {table}"):
        response = dynamodb.get_item(TableName=table, Key=budget_key(key), ConsistentRead=True)
    return response.get("Item")


#: The answer function writes the workspace into every budget item it creates
#: (``BUDGET_UPDATE_EXPRESSION`` in app/handler.py).
BUDGET_WORKSPACE_ATTRIBUTE = "workspace"


def workspace_budgets(dynamodb: Any, table: str, workspace: str) -> dict[str, dict[str, Any]]:
    """Every budget item of ``workspace``, by key hash, and not one of another workspace.

    A scan reads the whole table; it holds one item per key, on demand, and this runs when
    a customer leaves. An item preset by hand carries no workspace until its first
    question, so it is found only by the current key's hash (`read_budget`).
    """
    request: dict[str, Any] = {
        "TableName": table,
        "FilterExpression": "#workspace = :workspace",
        "ExpressionAttributeNames": {"#workspace": BUDGET_WORKSPACE_ATTRIBUTE},
        "ExpressionAttributeValues": {":workspace": {"S": workspace}},
        "ConsistentRead": True,
    }
    items: dict[str, dict[str, Any]] = {}
    while True:
        with explained(f"Reading the budget items of {workspace} in {table}"):
            response = dynamodb.scan(**request)
        for item in response.get("Items", []):
            if item.get(BUDGET_WORKSPACE_ATTRIBUTE, {}).get("S") != workspace:
                raise AwsProblem(
                    f"The scan for {workspace} returned a budget item of another workspace. "
                    "Nothing was deleted."
                )
            items[item[BUDGET_KEY_ATTRIBUTE]["S"]] = item
        if "LastEvaluatedKey" not in response:
            return items
        request["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def delete_budgets(dynamodb: Any, table: str, hashes: Iterable[str]) -> int:
    """Delete the budget items of ``hashes``; return how many."""
    count = 0
    for digest in hashes:
        with explained("Deleting a budget item"):
            dynamodb.delete_item(TableName=table, Key={BUDGET_KEY_ATTRIBUTE: {"S": digest}})
        count += 1
    return count


def workspace_documents(s3: Any, bucket: str, workspace: str) -> list[str]:
    """Every object under ``workspaces/<workspace>/``, and not one outside it.

    S3 lists by prefix, so this holds already; checking it again costs nothing, and a list
    that is about to be deleted is the one place where it must hold without exception.
    """
    prefix = documents_prefix(workspace)
    keys = remote_keys(s3, bucket, prefix)
    outside = [key for key in keys if not key.startswith(prefix)]
    if outside:
        raise AwsProblem(
            f"The listing of {prefix} returned {len(outside)} object(s) outside it, the first "
            f"{outside[0]}. Nothing was deleted."
        )
    return keys


def find_removal(
    workspace: str, settings: Mapping[str, str], ssm: Any, dynamodb: Any, s3: Any
) -> Removal:
    """Look up the key, the budget items and the documents of ``workspace``; delete nothing."""
    table = settings["BudgetTableName"]
    removal = Removal(workspace, key_parameter(settings["ApiKeyParameterPath"], workspace))
    removal.key = read_key(ssm, removal.parameter)
    if removal.key is not None:
        item = read_budget(dynamodb, table, removal.key)
        if item is not None:
            removal.budgets[key_hash(removal.key)] = item
    removal.budgets.update(workspace_budgets(dynamodb, table, workspace))
    removal.documents = workspace_documents(s3, settings["DocumentsBucketName"], workspace)
    return removal


def budget_line(item: Mapping[str, Any]) -> str:
    used = item.get("questions_used", {}).get("N", "0")
    limit = item.get("question_limit", {}).get("N")
    return f"{used} of {limit} questions used" if limit else f"{used} questions used"


def format_removal(removal: Removal) -> list[str]:
    """What belongs to the workspace — what a removal deletes."""
    lines = [f"Workspace {removal.workspace}:"]
    lines.append(
        f"  key        {removal.parameter}"
        if removal.key is not None
        else f"  key        none ({removal.parameter} does not exist)"
    )
    current = key_hash(removal.key) if removal.key is not None else None
    if removal.budgets:
        lines.append(f"  budget     {len(removal.budgets)} item(s):")
        lines += [
            f"    {budget_line(item)}" + (" (current key)" if digest == current else "")
            for digest, item in sorted(removal.budgets.items(), key=lambda pair: pair[0] != current)
        ]
    else:
        lines.append("  budget     none")
    prefix = documents_prefix(removal.workspace)
    if removal.documents:
        lines.append(f"  documents  {len(removal.documents)} object(s) under {prefix}:")
        lines += [f"    {key}" for key in removal.documents]
    else:
        lines.append(f"  documents  none under {prefix}")
    lines.append("  vectors    those of the documents, by an ingestion job after they are deleted")
    return lines


def carry_out(
    removal: Removal,
    settings: Mapping[str, str],
    ssm: Any,
    dynamodb: Any,
    s3: Any,
    log: Callable[[str], None] = print,
) -> bool:
    """Delete what `find_removal` found, and nothing else; True if a key was deleted.

    The budget items go before the key: the current key's item may be found only by the
    key's hash, so a run that stops halfway can be repeated and still finds it. After the
    key, the workspace's items are looked for once more: a question asked meanwhile writes
    a new one.
    """
    table = settings["BudgetTableName"]
    if removal.budgets:
        log(f"  {delete_budgets(dynamodb, table, removal.budgets)} budget item(s) deleted")
    if removal.key is not None:
        try:
            with explained(f"Deleting the key {removal.parameter}"):
                ssm.delete_parameter(Name=removal.parameter)
        except AwsProblem as problem:
            if error_code(problem.__cause__) != "ParameterNotFound":
                raise
        log(f"  key {removal.parameter} deleted")
    if removal.documents:
        delete_keys(s3, settings["DocumentsBucketName"], removal.documents)
        log(f"  {len(removal.documents)} object(s) deleted")
    late = workspace_budgets(dynamodb, table, removal.workspace)
    if late:
        log(f"  {delete_budgets(dynamodb, table, late)} budget item(s) written meanwhile, deleted")
    return removal.key is not None


#: ``KEY_CACHE_SECONDS`` in app/handler.py, in minutes.
KEY_CACHE_MINUTES = 5


# --- Command line --------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.workspace",
        description="Add a workspace's key, or remove a workspace from Andur.",
    )
    parser.add_argument("--stack-name", default=STACK_NAME, help=f"default: {STACK_NAME}")
    parser.add_argument("--region", default=REGION, help=f"default: {REGION}")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser(
        "add", help="create the workspace's key and print it once; never replaces a key"
    )
    add.add_argument("workspace")
    remove = commands.add_parser(
        "remove",
        help="show what belongs to the workspace; with --yes delete key, budget and documents",
    )
    remove.add_argument("workspace")
    remove.add_argument(
        "--yes", action="store_true", help="really delete; without it nothing is deleted"
    )
    return parser.parse_args(argv)


def run_add(args: argparse.Namespace, settings: Mapping[str, str], connect: Any) -> int:
    name = key_parameter(settings["ApiKeyParameterPath"], args.workspace)
    key = add_key(connect("ssm", args.region), name)
    # The key alone goes to stdout, everything else to stderr: `> file` keeps the key and
    # nothing else, `> /dev/null` keeps it off the screen.
    print(f"Stored a new key for workspace {args.workspace} under {name}.", file=sys.stderr)
    print(
        "It works at once and has the default budget (app/README.md, 'Keys are budgets, not "
        "passwords'). This is the only time this script prints it:",
        file=sys.stderr,
    )
    print(key)
    return 0


def run_remove(args: argparse.Namespace, settings: Mapping[str, str], connect: Any) -> int:
    ssm, dynamodb, s3 = (connect(name, args.region) for name in ("ssm", "dynamodb", "s3"))
    removal = find_removal(args.workspace, settings, ssm, dynamodb, s3)
    for line in format_removal(removal):
        print(line)
    if not args.yes:
        print("Nothing was deleted. To delete all of this, run again with --yes.")
        return 0

    print("Deleting:")
    key_deleted = carry_out(removal, settings, ssm, dynamodb, s3)
    job = run_ingestion_job(
        connect("bedrock-agent", args.region), settings["KnowledgeBaseId"], settings["DataSourceId"]
    )
    for line in format_job(job):
        print(line)
    if key_deleted:
        print(
            "Not quite finished: the answer function may accept the deleted key for up to "
            f"{KEY_CACHE_MINUTES} more minutes, and a question in that time writes a new budget "
            f"item. Run `python -m scripts.workspace remove {args.workspace} --yes` again after "
            f"{KEY_CACHE_MINUTES} minutes to delete it."
        )
    return 0 if job_succeeded(job) else 3


def main(argv: list[str] | None = None, connect: Callable[[str, str], Any] = client) -> int:
    utf8_output()
    args = parse_args(argv)
    problem = name_problem(args.workspace)
    if problem:
        print(f"error: {args.workspace!r}: {problem}", file=sys.stderr)
        return 1
    wanted = ADD_OUTPUTS if args.command == "add" else REMOVE_OUTPUTS
    try:
        settings = resolve(
            dict.fromkeys(wanted),
            lambda: read_stack_outputs(connect("cloudformation", args.region), args.stack_name),
        )
        if args.command == "add":
            return run_add(args, settings, connect)
        return run_remove(args, settings, connect)
    except AwsProblem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
