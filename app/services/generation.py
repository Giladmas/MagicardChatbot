import difflib
import re
import time

from app.config import settings
from app.runtime_config import DEFAULTS
from app.services import metrics
from app.services.conversation_history import Turn
from app.services.embeddings import get_openai_client
from app.services.retrieval import RetrievedChunk

REFUSAL = "I don't have information about that in the Magicard knowledge base."
FALLBACK_ERROR = "Sorry, I'm having trouble answering right now. Please try again shortly."

# Error-context answers must stay to the two-clause template described in
# SYSTEM_PROMPT; capping tokens well below the general answer budget backs that
# up structurally, since prompting alone doesn't fully pin down GPT's verbosity.
ERROR_CONTEXT_MAX_TOKENS = 80

# Whether a question is "a repeat" is decided here in code, not left to the model -
# testing showed GPT reliably over-applies a callback to any follow-up regardless of
# topic (e.g. adding "just to go over that again" to a brand-new question) once a
# conversation has a couple of turns. A plain text-similarity match against each
# prior question in this conversation is a cheap, deterministic signal instead.
_REPEAT_SIMILARITY_THRESHOLD = 0.5
_BACKREFERENCE_RE = re.compile(
    r"\b(again|earlier|before|previously|you said|you mentioned|you told me|last time)\b",
    re.IGNORECASE,
)


# Social messages are recognised in code, the same way repeats are - testing
# showed GPT answers them with the refusal string no matter how the prompt
# describes small talk. Known social phrases ("ty", "it worked", "have a nice
# day", "bye", ...) are stripped from the message; if nothing is left but filler
# words and emoji, it's small talk, and the parts found are passed to GPT so the
# reply covers each of them ("ty it worked, have a nice day and bye"). Anything
# left over ("thanks, and what's the fee?") means a real question - answer it.
_SMALL_TALK_PHRASES = [
    ("frustration", r"(that|this|it|you|u) (didn'?t|did not|doesn'?t|does not|don'?t|do not) help( at all| me)?"),
    ("frustration", r"(not|wasn'?t|isn'?t|not very) (helpful|useful)"),
    ("frustration", r"(you'?re|you are|ur|u r|this is|that'?s|that is) (so |really )?(useless|not helpful|no help|terrible|bad|stupid|dumb|annoying)"),
    ("frustration", r"useless|not what i asked"),
    ("identity", r"(are|r) (you|u) (a |an )?(real )?(bot|robot|human|person|ai|machine)"),
    ("identity", r"(is this|am i (talking|speaking|chatting) (to|with)) (a |an )?(real )?(bot|robot|human|person|ai)"),
    ("identity", r"(who|what) (are|r) (you|u)"),
    ("greeting", r"good (morning|afternoon|evening)"),
    ("greeting", r"how (are|r) (you|u|ya)( doing)?( today)?|how'?s it going|how (have|ve) you been"),
    ("greeting", r"what'?s up|wh?ass?up|hope (you'?re|you are|u r) (well|good|doing well)"),
    ("closing", r"good ?night"),
    ("closing", r"have an? (nice|good|great|lovely|wonderful|awesome|blessed|nice rest of your|good rest of your) (day|one|night|evening|weekend|week|time)( ahead)?"),
    ("closing", r"(you|u) too|same to (you|u)"),
    ("closing", r"see (you|ya|u)( (later|soon|around|tomorrow))?|take care|catch (you|ya) later|talk (to (you|u) )?(later|soon)"),
    ("closing", r"(that'?s|thats|that is) (all|it)( (i|for) (needed|need|now|today))?|nothing else|no more questions"),
    ("closing", r"(i'?m|im|i am) (good|all set|done|fine)( now| for now)?|all set|no (thanks|thank you|need)"),
    ("thanks", r"thank(s|x|z)*( (you|u|ya|yo))?"
               r"( (so|very) much| a (lot|bunch|ton|million)| again"
               r"| for (the|your|all the|all your|all) (help|info|information|answer|reply|time|support|assistance|patience|quick reply))*"),
    ("thanks", r"much appreciated|many thanks|(i )?(really )?appreciate (it|that|this|you|the help|your help)"),
    ("thanks", r"(you'?re|you are|ur) (the best|great|awesome|amazing|a star|a legend|a life ?saver|(so |very )?helpful)|(you|u) rock|life ?saver"),
    ("resolved", r"(it|that|this|everything|all)( now)? (worked|works|work(ed)? now|helped|helps|did the trick|fixed it|solved it)"),
    ("resolved", r"(it'?s|its|it is|everything'?s|everything is) (working|fixed|sorted|solved|resolved|done|good)( now)?"),
    ("resolved", r"(did|does) the trick|problem solved|all (good|sorted)|got it( now)?|gotcha|i see|makes sense|(i )?understand( now)?"),
    ("resolved", r"(that'?s|thats|that is) (helpful|clear|perfect|great|good|fine|awesome)"),
]
_SMALL_TALK_PHRASE_RES = [(cat, re.compile(rf"\b(?:{p})\b")) for cat, p in _SMALL_TALK_PHRASES]

_SMALL_TALK_WORD_CATEGORIES = {
    "greeting": {"hi", "hello", "hey", "heya", "hiya", "yo", "sup", "gm", "morning", "hola", "greetings", "howdy"},
    "closing": {"bye", "goodbye", "byebye", "cya", "bb", "ttyl", "gn", "later", "farewell"},
    "thanks": {"thanks", "thank", "thx", "thnx", "thanx", "tnx", "tx", "ty", "tysm", "tyvm", "tq", "cheers", "appreciated"},
    "resolved": {"worked", "works", "working", "fixed", "sorted", "solved", "resolved", "understood", "noted"},
}
# Acknowledgements and filler - fine inside a social message, never meaningful on their own.
_SMALL_TALK_FILLER = {
    "ok", "okay", "okey", "okk", "k", "kk", "alright", "aight", "cool", "nice", "great",
    "perfect", "awesome", "amazing", "excellent", "wonderful", "fantastic", "brilliant",
    "sweet", "good", "fine", "yes", "yeah", "yep", "yup", "ya", "yea", "sure", "no",
    "nope", "nah", "wow", "oh", "ah", "hmm", "yay", "finally", "lol", "lmao", "lmfao",
    "rofl", "xd", "bro", "man", "dude", "mate", "sir", "buddy", "friend", "fam", "and",
    "so", "too", "also", "again", "now", "it", "it's", "its", "is", "that", "thats",
    "that's", "this", "all", "everything", "then", "well", "just", "really", "very",
    "much", "you", "u", "i", "im", "i'm", "a", "the", "for", "done", "anyway", "btw",
}
_LAUGH_RE = re.compile(r"^(a?(ha|he|hi)+h?|l+o+l+)$")
_SMALL_TALK_PART_LABELS = {
    "greeting": "a greeting",
    "thanks": "thanks",
    "resolved": "that it worked / they understand now",
    "closing": "a goodbye or sign that they're done",
    "frustration": "frustration with your answer",
    "identity": "a question about who or what you are",
    "ack": "a simple acknowledgement",
}


_STOCK_CLOSER_RE = re.compile(
    r"(?<=[.!?])\s+[^.!?]*\b(feel free to|if you have any (more |other |further )?questions)\b[^.!?]*[.!?]*\s*$",
    re.IGNORECASE,
)


def _small_talk_word_category(word: str) -> str | None:
    # Try the word as typed, then with stretched letters squashed ("byeee", "coool").
    for w in (word, re.sub(r"(.)\1{2,}", r"\1\1", word), re.sub(r"(.)\1+", r"\1", word)):
        for cat, words in _SMALL_TALK_WORD_CATEGORIES.items():
            if w in words:
                return cat
        if w in _SMALL_TALK_FILLER or _LAUGH_RE.match(w):
            return "ack"
    return None


def _small_talk_parts(question: str) -> list[str] | None:
    """The social parts of a message that is only small talk, else None."""
    text = question.lower().replace("’", "'")
    if re.search(r"\d", text):
        return None
    found: list[str] = []
    for cat, pattern in _SMALL_TALK_PHRASE_RES:
        text, n = pattern.subn(" ", text)
        if n and cat not in found:
            found.append(cat)
    for word in re.findall(r"[a-z']+", text):
        cat = _small_talk_word_category(word.strip("'"))
        if cat is None:
            return None
        if cat not in found:
            found.append(cat)
    if len(found) > 1 and "ack" in found:
        found.remove("ack")
    # "is it working?" is a question; only "how are you?"/"are you a bot?" may ask one.
    if "?" in question and not {"greeting", "identity", "frustration"} & set(found):
        return None
    return found or ["ack"]  # nothing but emoji/punctuation (":)", "👍")


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _detect_repeat(question: str, history: list[Turn] | None) -> Turn | None:
    if not history:
        return None
    best_turn, best_score = None, 0.0
    for turn in history:
        score = _similarity(question, turn.question)
        if score > best_score:
            best_score, best_turn = score, turn
    if best_score >= _REPEAT_SIMILARITY_THRESHOLD:
        return best_turn
    if best_turn is not None and _BACKREFERENCE_RE.search(question):
        return best_turn
    return None


SYSTEM_PROMPT = (
    "You are the Magicard support assistant. Answer the user's question using ONLY "
    "the context chunks below - never use outside knowledge. You may paraphrase, "
    "combine, and draw direct conclusions from the context (e.g. if the context says "
    "a type of transaction cannot normally be reversed, that answers a question about "
    "cancelling it) - you don't need a verbatim sentence that matches the question.\n"
    "RULE, checked first, before anything else below: each user message ends with a "
    "bracketed note like \"(conversation note: ...)\" telling you, as ground truth "
    "decided outside your judgment, whether this is a genuine repeat of an earlier "
    "question in this conversation. Trust that note completely - do not use a "
    "callback unless the note explicitly says this question is a repeat, and never "
    "skip a callback the note tells you to use. When the note confirms a repeat, "
    "open with a short natural line acknowledging it - vary the wording each time "
    "instead of reusing the same phrase (\"Like I mentioned before, ...\", \"As I "
    "mentioned earlier, ...\", \"Same as before - ...\", \"Just to go over that "
    "again, ...\", \"As I advised, ...\") - then give the answer, and never repeat "
    "the exact same sentence used for that question earlier. This rule never "
    "applies to the exact refusal string below - see that rule instead.\n"
    "If a \"User's recent error\" block is present, answer in exactly two clauses "
    "joined by a comma or dash: (1) a short plain-language reason, under 10 words, "
    "with no technical details from the block (no provider names, HTTP status codes, "
    "error codes, \"Bad Request\", timestamps, or system wording); (2) the matching "
    "context chunk's guidance copied almost word-for-word. Nothing before, between, "
    "or after those two clauses - no extra sentence of elaboration, no restating the "
    "question, no sign-off. Example: \"That happened because your profile details "
    "didn't go through - please verify your account and profile information are "
    "complete and try again. If the issue persists, contact support.\"\n"
    "SMALL TALK: if the user's message has no actual Magicard question in it - just "
    "social chat in any wording, slang, abbreviation, emoji, or combination (thanks, "
    "\"ty\", \"thx\", a greeting, \"how are you\", a goodbye, \"have a nice day\", "
    "\"it worked\", \"ok\", \"cool\", \"lol\", \"no that's all\", a compliment, a "
    "question about who you are, or frustration like \"that didn't help\") - reply "
    "with ONE short, warm, natural sentence (two at most) written for exactly what "
    "they said, like a friendly human agent would:\n"
    "- Respond to every part of the message in that one reply, not just the first "
    "part: \"ty it worked, have a nice day and bye\" -> \"So glad it worked! You have "
    "a great day too - bye!\"\n"
    "- Echo their tone and details (\"it worked\" -> glad it's sorted; \"have a nice "
    "day\" -> wish it back; \"how are you\" -> answer briefly, then offer help).\n"
    "- Only greet (\"Hi!\") when they greeted you. An acknowledgement like \"ok\", "
    "\"cool\", \"lol\" or an emoji is not a greeting - answer it lightly and "
    "naturally (\"Anytime!\", \"Glad that helped!\", \"Happy to help!\"), and never "
    "describe what they sent (no \"thanks for your message\", \"glad to see your "
    "thumbs up\").\n"
    "- If they're saying goodbye or that they need nothing else, close warmly and "
    "don't ask whether they need anything else.\n"
    "- Frustration or a complaint about your answer: apologise briefly, then invite "
    "them to tell you more about the problem or to contact Magicard support.\n"
    "- Who/what are you: you're Magicard's virtual support assistant, here to help "
    "with Magicard questions.\n"
    "- Vary the wording naturally; don't reuse one stock phrase for everything. An "
    "offer of more help is optional and short (\"Anything else I can help with?\"), "
    "never the generic \"If you have any (more) questions, feel free to ask / let "
    "me know\" line.\n"
    "This overrides the error-block rule above and the refusal rule below - never "
    "use either for small talk, and never re-explain an earlier answer. If the "
    "message mixes small talk with a real question, answer the question as normal "
    "(a short friendly touch is fine).\n"
    f'If the user asks an actual question or request (not small talk - a message '
    f'that is only "ok", "lol", "nope", an emoji, etc. is always small talk) and '
    f'neither the context nor the error block has anything relevant, your ENTIRE '
    f'reply must be exactly: "{REFUSAL}" - nothing before it, nothing after it, no '
    f'callback, no matter what the conversation note says.\n'
    "Keep every answer brief - one sentence in almost every case, two at most. Never "
    "use filler that adds length without information: \"it looks like\", \"it seems\", "
    "\"the error message indicates\", \"this could be due to\", \"to resolve this\", "
    "\"I recommend\", \"feel free to\", \"for further assistance\", or similar. Be "
    "warm and human, not wordy or robotic: no flat acknowledgements like "
    "\"Understood\"/\"Noted\", and no padding just to sound friendlier - a natural, "
    "conversational word choice is enough on its own.\n"
    "Brevity never means dropping figures: whenever the answer mentions an amount, "
    "fee, minimum, maximum, or limit that the context states, give the exact figure "
    "(\"below the $10 minimum\", \"the 3% + $3 fee\", \"3 active cards\"), never a "
    "vague stand-in like \"the minimum\" or \"the maximum number\". Likewise, when "
    "the context gives a list of reasons or steps (e.g. why something failed), "
    "include every item on that list and its closing advice (such as contacting "
    "support) - never shorten it with \"such as\" or \"several reasons\"."
)


def _build_context(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(f"[{c.source}]\n{c.text}" for c in chunks)


def _build_error_context(ctx) -> str:
    if not ctx:
        return ""
    parts = [
        p
        for p in [
            f"failed_action={ctx.action}" if getattr(ctx, "action", None) else None,
            f"provider={ctx.provider}" if ctx.provider else None,
            f"error_code={ctx.error_code}" if ctx.error_code else None,
            f"message={ctx.message}" if ctx.message else None,
            f"occurred_at={ctx.created_at}" if ctx.created_at else None,
        ]
        if p
    ]
    return "User's recent error:\n" + "; ".join(parts) if parts else ""


def generate_answer(
    question: str,
    chunks: list[RetrievedChunk],
    history: list[Turn] | None = None,
    max_tokens: int = DEFAULTS["max_answer_tokens"],
    temperature: float = DEFAULTS["temperature"],
    error_context=None,
) -> str:
    context = _build_context(chunks)
    error_block = _build_error_context(error_context)
    repeat_turn = _detect_repeat(question, history)
    small_talk_parts = _small_talk_parts(question)
    if small_talk_parts is not None:
        parts = ", ".join(_SMALL_TALK_PART_LABELS[p] for p in small_talk_parts)
        conversation_note = (
            f"(conversation note: this message is only small talk, not a question. It "
            f"contains: {parts}. Reply per the SMALL TALK rule, responding to each of "
            "those parts; never use the refusal string or a callback."
        )
        if "greeting" not in small_talk_parts:
            conversation_note += " They did not greet you, so don't open with a greeting."
        if small_talk_parts == ["ack"]:
            conversation_note += (
                " Reply in a few words (\"Anytime!\", \"Happy to help!\", \"Glad that "
                "helped!\") and don't comment on their message itself (no \"glad to see "
                "your message/response/thumbs up\")."
            )
        if "closing" in small_talk_parts:
            conversation_note += (
                " They're wrapping up - close warmly, don't offer more help or ask if "
                "they need anything else."
            )
        conversation_note += (
            " Never end with \"If you have any (more) questions, feel free to ask / "
            "just let me know\".)"
        )
    elif repeat_turn is not None:
        conversation_note = (
            "(conversation note: this question IS a repeat of an earlier one in "
            f'this conversation ("{repeat_turn.question}"). Open with a brief '
            "varied callback acknowledging that, then answer - unless the refusal "
            "rule applies, which always wins.)"
        )
    else:
        conversation_note = (
            "(conversation note: this question is NOT a repeat of anything asked "
            "before in this conversation, even if earlier turns are shown above - "
            "answer plainly, no callback.)"
        )
    user_message = (
        f"Context:\n{context}\n\n{error_block}\n\nQuestion: {question}\n\n{conversation_note}"
        if error_block
        else f"Context:\n{context}\n\nQuestion: {question}\n\n{conversation_note}"
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history or []:
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})
    messages.append({"role": "user", "content": user_message})

    start = time.monotonic()
    response = get_openai_client().chat.completions.create(
        model=settings.openai_chat_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    latency_ms = (time.monotonic() - start) * 1000
    answer = response.choices[0].message.content.strip()

    # Hard guarantee independent of prompt compliance: chat.py's cache/miss-log
    # logic depends on an exact REFUSAL match, so never let a stray callback or
    # other prefix/suffix around the refusal text break that contract.
    if REFUSAL in answer and answer != REFUSAL:
        answer = REFUSAL
    # Same idea for small talk: GPT tacks a canned "If you have any questions, feel
    # free to ask." onto most social replies despite the prompt, so drop it when
    # there's a real reply before it.
    if small_talk_parts is not None:
        trimmed = _STOCK_CLOSER_RE.sub("", answer).strip()
        if trimmed:
            answer = trimmed

    usage = response.usage
    metrics.record_generation(
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        latency_ms=latency_ms,
        refusal=(answer == REFUSAL),
        small_talk=small_talk_parts is not None,
        truncated=response.choices[0].finish_reason == "length",
    )
    return answer
