# app — the Lambda handler

Empty on purpose. Build step 3 puts the question handler here: take a
question, retrieve from the knowledge base with a `workspace` filter, ask
the answering model, return the answer with its sources and the layer each
source came from.

Deployed as a Lambda function with a Function URL, so there is nothing to
pay for while nobody asks anything.
