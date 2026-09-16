# scripts — operating the knowledge base

Empty on purpose. Build step 2 puts `ingest` here: walk a local folder,
upload each file plus its sidecar metadata to
`s3://<documents bucket>/workspaces/<workspace>/<layer>/`, then start an
ingestion job and wait for it to finish.

Until then, the same job can be started by hand:

```bash
aws bedrock-agent start-ingestion-job \
  --knowledge-base-id <KnowledgeBaseId> \
  --data-source-id <DataSourceId> \
  --region us-east-1
```
