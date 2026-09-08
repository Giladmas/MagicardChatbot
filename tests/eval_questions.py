"""Golden eval set: (question, expected_source_file) pairs.

Used by tests/test_retrieval.py to check that Qdrant returns a chunk from the
right knowledge doc for a representative question, before GPT generation is
added on top. Extend this list as real user questions come in after launch.
"""

EVAL_QUESTIONS: list[tuple[str, str]] = [
    ("What is Magicard?", "faq.txt"),
    ("What currency is my wallet in?", "faq.txt"),
    ("How do I deposit funds?", "deposits.txt"),
    ("What network should I use to send USDT?", "deposits.txt"),
    ("My deposit hasn't shown up, what do I do?", "deposits.txt"),
    ("How do I withdraw money?", "withdrawals.txt"),
    ("Can I cancel a withdrawal once it's sent?", "withdrawals.txt"),
    ("How do I create a virtual card?", "cards.txt"),
    ("Why was my card declined?", "cards.txt"),
    ("What is KYC?", "kyc.txt"),
    ("My verification is stuck pending, what should I do?", "kyc.txt"),
    ("Should I share my CVV with the chatbot?", "account_security.txt"),
    ("Is there a fee for withdrawals?", "fees_limits.txt"),
    ("What is my current withdrawal limit?", "fees_limits.txt"),
    # out-of-scope: retrieval will still return something, but nothing should
    # score highly enough / the generation layer must refuse (checked later,
    # once GPT is wired up).
    ("What's the weather like today?", "__none__"),
]
