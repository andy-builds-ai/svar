"""The topics Svar refuses, read by the stack's topic guardrail and by the topic model.

The stack builds the guardrail from this tuple; the answer function, packaged from this folder,
builds the topic model's prompt from it (topic_model.py). One tuple, so the two ways of
checking never drift apart.
"""

#: Name, definition and examples of each topic, in the order Bedrock reads them; the order
#: changes verdicts too. A broad name such as "Legal advice" refused questions about the
#: business's own contracts and prices. Measure every change with `python -m
#: scripts.topic_probe` before it is deployed (docs/decisions.md).
DENIED_TOPICS = (
    (
        "Legal action",
        "Questions about taking or facing legal action: suing someone or being sued, going to "
        "court, hiring a lawyer, and whether a court would find an act legal, illegal or "
        "enforceable.",
        [
            "Can I sue my landlord over this?",
            "Is this non-compete clause enforceable?",
            "Can I take my neighbour to court over the noise?",
        ],
    ),
    (
        "Tax returns",
        "Questions about a person's own tax return and the tax office: what money they get back "
        "through a tax return, what income they must declare, and how to pay less tax or hide "
        "money from the tax office.",
        [
            "How do I pay less income tax?",
            "Do I have to declare this income?",
            "Do I need to file a tax return this year?",
        ],
    ),
    (
        "Medical advice",
        "Questions about a person's health: what a symptom, pain, rash or injury means, whether "
        "it is serious or needs a doctor, and which medicine, dose, remedy or treatment to take.",
        [
            "What should I take for a headache?",
            "Is this rash serious?",
            "How much ibuprofen can I take?",
            "Was kann ich gegen den Kater einwerfen?",
        ],
    ),
    (
        "Private lives",
        "Questions about the private life of a person: their partner, family, health, home, "
        "wealth, political views or religious beliefs; and questions about celebrities, "
        "politicians or other public figures.",
        [
            "Who is the richest person in the world?",
            "Is our CEO married?",
            "Where does my colleague live?",
            "Welcher Religion gehört mein Kollege an?",
        ],
    ),
)
