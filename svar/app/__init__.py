"""Svar's answer function: handler.py and the modules beside it.

Neither Lambda nor the tests import this folder as a package: both import
the modules by their plain names, handler, aws, plans and the rest
(tests/conftest.py). This file is there for scripts/workspace.py, which reads
the plans as ``app.plans``, and for the stack, which builds the topic
guardrail from ``app.denied_topics``; without it `cdk synth` fails."""
