"""ask_user: the lead agent asks the user a question when it is unsure.

The model may offer up to three options; the first is its recommendation and a final
"type my own answer" option is always added. Without options the user gets a plain input box.

Runs that the user did not start (woken by a schedule, a finished background job or a teammate)
may have nobody watching. There the question is withdrawn after ``askUser.unattendedTimeout``
seconds (default 300) and reported as unanswered. Runs with no interactive client never show
the question at all.
"""

from __future__ import annotations

from ..agent.types import AgentTool, ToolContext, ToolResult
from ..harness.extensions import ExtensionAPI
from ..harness.ui import TIMED_OUT

LETTERS = "ABCD"
MAX_OPTIONS = 3
DEFAULT_UNATTENDED_TIMEOUT = 300.0

NO_USER = "The question was not answered: this session is not interactive."
DECLINED = "The user dismissed the question without answering."

DESCRIPTION = (
    "Ask one question and wait for the user. Offer up to 3 options (the first is shown as recommended), "
    "or omit options for free text. A custom-answer choice is always available. "
    "Answers are saved in the conversation; do not request secrets. "
    "Timeout, dismissal and non-interactive sessions return answered=false without an answer."
)


def _timeout(api: ExtensionAPI) -> float:
    value = api.settings.get("askUser.unattendedTimeout", DEFAULT_UNATTENDED_TIMEOUT)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return DEFAULT_UNATTENDED_TIMEOUT
    return max(0.0, float(value))


def _wait_text(seconds: float, t=None) -> str:
    from ..i18n import Translator

    t = t or Translator()
    if seconds >= 60 and seconds % 60 == 0:
        n = int(seconds // 60)
        return t("features.ask_user.minute" if n == 1 else "features.ask_user.minutes", count=n)
    return t("features.ask_user.seconds", seconds=seconds)


def setup(api: ExtensionAPI) -> None:
    state = {"unattended": False}

    def run_started(rt, prompts) -> None:
        if rt.role == "main":
            state["unattended"] = bool(prompts) and all(
                getattr(p, "kind", None) == "notification" for p in prompts
            )

    def user_present(_text, _session) -> None:
        # Steering is delivered only after the current tool batch, so react when it is queued:
        # a question in that batch must not time out on a user who is typing.
        state["unattended"] = False
        api.ui.cancel_input_timeouts()

    def execute(args: dict, ctx: ToolContext) -> ToolResult:
        question = args["question"].strip()
        options = [
            {"label": str(o["label"]).strip(), "description": str(o.get("description") or "").strip()}
            for o in args.get("options") or []
        ]
        if not question:
            return ToolResult.error(ctx.t("features.ask_user.empty"))
        if any(not o["label"] for o in options):
            return ToolResult.error(ctx.t("features.ask_user.empty_option"))
        ui = api.ui
        if not getattr(ui, "interactive", False):
            return ToolResult.text(
                ctx.t("features.ask_user.no_user"), details={"question": question, "answered": False}
            )
        timeout = _timeout(api) if state["unattended"] else None
        details: dict = {"question": question, "options": [o["label"] for o in options]}

        def declined() -> ToolResult:
            if ctx.signal.is_set():
                return ToolResult.text(
                    ctx.t("features.ask_user.cancelled"),
                    is_error=True,
                    details={**details, "answered": False, "cancelled": True},
                )
            return ToolResult.text(
                ctx.t("features.ask_user.declined"), details={**details, "answered": False}
            )

        def timed_out() -> ToolResult:
            return ToolResult.text(
                ctx.t("features.ask_user.timed_out", duration=_wait_text(timeout, ctx.t)),
                details={**details, "answered": False, "timed_out": True},
            )

        if options:
            labels = []
            for i, o in enumerate(options):
                label = f"{LETTERS[i]}. {o['label']}" + (
                    ctx.t("features.ask_user.recommended") if i == 0 else ""
                )
                labels.append(label + (f" — {o['description']}" if o["description"] else ""))
            custom = ctx.t("features.ask_user.custom", letter=LETTERS[len(options)])
            picked = ui.select(question, [*labels, custom], timeout=timeout)
            if picked is TIMED_OUT:
                return timed_out()
            state["unattended"] = False
            if picked is None:
                return declined()
            if picked != custom:
                i = labels.index(picked)
                return ToolResult.text(
                    ctx.t("features.ask_user.chose", letter=LETTERS[i], answer=options[i]["label"]),
                    details={
                        **details,
                        "answered": True,
                        "choice": LETTERS[i],
                        "answer": options[i]["label"],
                    },
                )
            # The user is present once they picked the custom option: wait for them to type.
            timeout = None
        answer = ui.input(question, timeout=timeout)
        if answer is TIMED_OUT:
            return timed_out()
        state["unattended"] = False
        if answer is None or not answer.strip():
            return declined()
        answer = answer.strip()
        return ToolResult.text(
            ctx.t("features.ask_user.answered", answer=answer),
            details={**details, "answered": True, "answer": answer},
        )

    api.register_tool(
        AgentTool(
            name="ask_user",
            label="Ask user",
            description=DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete question, understandable on its own",
                    },
                    "options": {
                        "type": "array",
                        "maxItems": MAX_OPTIONS,
                        "description": "Optional choices, recommended first. A free-text choice is added automatically.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": "Short choice text",
                                },
                                "description": {"type": "string", "description": "What choosing it means"},
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["question"],
            },
            execute=execute,
            read_only=True,
            sequential=True,
            i18n_key="tools.ask_user",
        ),
        roles=("main",),
    )
    api.on("before_agent_start", run_started)
    api.on("user_steer", user_present)
