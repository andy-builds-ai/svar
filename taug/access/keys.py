"""The workspace keys: read from Parameter Store, cached per instance, compared in constant time.

Taug's key check, packed into the function that checks keys (Svar's answer function). It imports
nothing from that function: the caller brings how to make the SSM client and where to log
(`KeyStore`), and turns the three exceptions below into its own answers."""

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from taug.access import aws_errors as aws
from taug.access.workspaces import WORKSPACE_NAME

KEY_CACHE_SECONDS = 300

#: How long a key may go on deciding after its last good read while it cannot be
#: read again. Past this, its workspace is `KeyUnreadable` until Parameter Store answers.
KEY_MAX_AGE_SECONDS = 3 * KEY_CACHE_SECONDS

KEY_RELOAD_MIN_SECONDS = 10

#: Reads per `KEY_RELOAD_MIN_SECONDS` and function instance for workspaces it holds
#: no key for. Five instances (Ireland's cap) at this rate are 5 reads a second, an eighth
#: of the 40 a second the account's Parameter Store allows by default for all callers; ten
#: (Frankfurt's reservation) are 10, a quarter.
UNKNOWN_KEY_READS = 10

#: Reads per `KEY_RELOAD_MIN_SECONDS` and function instance because a presented key
#: is not the cached one. The brake per workspace alone would let wrong keys sent
#: round many real workspaces spend the account's reads.
WRONG_KEY_READS = 10


#: Seconds that only run forward: how old a cached key is. A test sets it, as it sets the
#: caller's clock.
clock: Callable[[], float] = time.monotonic


class KeyUnreadable(Exception):
    """A key could not be read, now or moments ago: the caller is set up wrong, or Parameter
    Store does not answer. Svar answers 500."""


class KeyReadsSpent(Exception):
    """This instance has used its reads for workspaces it holds no key for.

    Not `WrongKey`, which would say the key is wrong without looking: the check holds
    back for a few seconds. Svar answers 503 with ``Retry-After``, not 429, which there
    means a key's own quota.
    """

    retry_after_seconds = KEY_RELOAD_MIN_SECONDS


class WrongKey(Exception):
    """The presented key is not the requested workspace's key, or the workspace has none.
    Svar answers 401."""


@dataclass(frozen=True)
class KeyStore:
    """Where the keys are read, and what the caller lends the check to read them."""

    #: Parameter Store path of the keys, one SecureString each at ``<path>/<workspace>``.
    path: str
    region: str
    #: Makes an AWS client: ``make_client("ssm", region)``. It raises `KeyUnreadable` when
    #: it cannot.
    make_client: Callable[[str, str], Any]
    #: Writes one event to the caller's log: ``log_event(event, **fields)``.
    log_event: Callable[..., None]


def read_workspace_key(store: KeyStore, workspace: str) -> str | None:
    """The key of one workspace, read and decrypted from ``<path>/<workspace>``.

    None when Parameter Store holds no such parameter: an answer, not an outage.
    """
    if not WORKSPACE_NAME.match(workspace):
        # The caller checks the name first; without the check a name could leave the path.
        raise ValueError("Only a checked workspace name may name a key.")
    ssm = store.make_client("ssm", store.region)
    try:
        response = ssm.get_parameter(Name=f"{store.path}/{workspace}", WithDecryption=True)
    except aws.botocore_errors() as exc:
        if aws.error_code(exc) == "ParameterNotFound":
            return None
        store.log_event("keys-unreadable", workspace=workspace, detail=aws.error_detail(exc))
        raise KeyUnreadable(
            f"The key of the workspace {workspace} could not be read: {aws.error_cause(exc)}."
        ) from exc
    return response["Parameter"]["Value"]


@dataclass
class KnownKey:
    """What a warm function instance last read about one workspace's key, and when."""

    #: None: Parameter Store has no key for the workspace.
    key: str | None
    read_at: float
    tried_at: float


#: Workspace name to what was read under it. Only ever filled from ``<path>/<name>``.
_key_cache: dict[str, KnownKey] = {}

#: Workspace name to when reading its key last failed, for names not in `_key_cache`.
_failed_reads: dict[str, float] = {}

#: When this instance read for a workspace it held no key for, within the last
#: `KEY_RELOAD_MIN_SECONDS`.
_unknown_reads: list[float] = []

#: When this instance read again because a presented key was not the cached one,
#: within the last `KEY_RELOAD_MIN_SECONDS`.
_wrong_key_reads: list[float] = []


def same_key(presented: str, stored: str | None) -> bool:
    """Whether the presented key is the stored one, compared in constant time."""
    if stored is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), stored.encode("utf-8"))


def forget_unknown_workspaces(now: float) -> None:
    """Drop the names found without a key that no longer hold back a read.

    Without this, a stream of invented names would grow the cache for as long as
    the instance lives.
    """
    for workspace in [
        workspace
        for workspace, known in _key_cache.items()
        if known.key is None and now - known.tried_at >= KEY_RELOAD_MIN_SECONDS
    ]:
        del _key_cache[workspace]


def count_unknown_read(now: float) -> None:
    """Allow one more read for a workspace this instance holds no key for, or refuse it
    (`KeyReadsSpent`).

    At most `UNKNOWN_KEY_READS` every `KEY_RELOAD_MIN_SECONDS`: a stream of invented
    names must not spend the account's Parameter Store reads, which the workspaces need
    for their first reads too. A key in the cache never comes here.
    """
    _unknown_reads[:] = [at for at in _unknown_reads if now - at < KEY_RELOAD_MIN_SECONDS]
    if len(_unknown_reads) >= UNKNOWN_KEY_READS:
        raise KeyReadsSpent(f"More than {UNKNOWN_KEY_READS} keys in {KEY_RELOAD_MIN_SECONDS} s.")
    _unknown_reads.append(now)


def may_read_for_wrong_key(now: float) -> bool:
    """Whether one more read because of a key that is not the cached one is allowed.

    At most `WRONG_KEY_READS` every `KEY_RELOAD_MIN_SECONDS`, across workspaces. Over
    the cap the cached key decides: a wrong key is still `WrongKey`, and only a key that
    was just changed waits a few seconds longer.
    """
    _wrong_key_reads[:] = [at for at in _wrong_key_reads if now - at < KEY_RELOAD_MIN_SECONDS]
    if len(_wrong_key_reads) >= WRONG_KEY_READS:
        return False
    _wrong_key_reads.append(now)
    return True


def reload_workspace_key(store: KeyStore, workspace: str) -> str | None:
    """Read one workspace's key now and keep it, or keep that there is none.

    A read that fails is remembered: in the cached entry, or for a name without
    one in `_failed_reads`, so the next read waits `KEY_RELOAD_MIN_SECONDS`.
    """
    tried_at = clock()
    known = _key_cache.get(workspace)
    if known is not None:
        known.tried_at = tried_at
    try:
        key = read_workspace_key(store, workspace)
    except KeyUnreadable:
        failed_at = clock()
        if known is not None:
            known.tried_at = failed_at
        else:
            # Dropping what no longer holds back a read keeps this as small as the reads.
            for name, at in list(_failed_reads.items()):
                if failed_at - at >= KEY_RELOAD_MIN_SECONDS:
                    del _failed_reads[name]
            _failed_reads[workspace] = failed_at
        raise
    _failed_reads.pop(workspace, None)
    _key_cache[workspace] = KnownKey(key=key, read_at=clock(), tried_at=tried_at)
    forget_unknown_workspaces(tried_at)
    return key


def current_key(presented: str, workspace: str, store: KeyStore) -> str | None:
    """The workspace's key as this request decides with it, read at most once per request.

    It is read when this instance has never read it, when it is `KEY_CACHE_SECONDS`
    old, or when it is not the presented key, so a new key works at once; the
    last two at most every `KEY_RELOAD_MIN_SECONDS` per workspace. A key that
    cannot be read again goes on deciding until it is `KEY_MAX_AGE_SECONDS` old;
    then it is `KeyUnreadable`. A read for a workspace without a key in the cache
    counts against `UNKNOWN_KEY_READS`, a read because the presented key is not the
    cached one against `WRONG_KEY_READS`.
    """
    now = clock()
    known = _key_cache.get(workspace)
    if known is None:
        failed_at = _failed_reads.get(workspace)
        if failed_at is not None and now - failed_at < KEY_RELOAD_MIN_SECONDS:
            raise KeyUnreadable(
                f"The key of the workspace {workspace} could not be read moments ago."
            )
        count_unknown_read(now)
        return reload_workspace_key(store, workspace)
    age = now - known.read_at
    stale = age >= KEY_CACHE_SECONDS
    if (stale or not same_key(presented, known.key)) and (
        now - known.tried_at >= KEY_RELOAD_MIN_SECONDS
    ):
        if known.key is None:
            count_unknown_read(now)
        elif not stale and not may_read_for_wrong_key(now):
            return known.key
        try:
            return reload_workspace_key(store, workspace)
        except KeyUnreadable as error:
            store.log_event(
                "keys-reload-failed", workspace=workspace, error=str(error), stale=stale
            )
            if age >= KEY_MAX_AGE_SECONDS:
                raise
    if age >= KEY_MAX_AGE_SECONDS:
        raise KeyUnreadable(
            f"The key of the workspace {workspace} is past its "
            f"{KEY_MAX_AGE_SECONDS} seconds and could not be read again."
        )
    return known.key


def authorize(key: str, workspace: str, store: KeyStore) -> None:
    """Refuse a key that is not the requested workspace's key (`WrongKey`, 401 in Svar).

    A wrong key, another workspace's key and a workspace without a key get the
    same sentence. Status and timing can still tell a cached workspace from an
    unknown name (`KeyReadsSpent` reaches only names without a cached key; `KeyUnreadable`
    those, and a cached key that is past `KEY_MAX_AGE_SECONDS` and cannot be read again, see
    `current_key`), so workspace names are not a secret.
    """
    stored = current_key(key, workspace, store)
    if not same_key(key, stored):
        raise WrongKey(f"This key does not open the workspace {workspace}.")
