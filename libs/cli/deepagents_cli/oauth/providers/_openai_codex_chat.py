"""ChatOpenAI subclass tailored for the ChatGPT Codex Responses endpoint.

The Codex backend rejects any `{"role": "system", ...}` item inside
`input`, returning HTTP 400
`{'detail': 'System messages are not allowed'}`. The system prompt
must travel as the top-level `instructions` field instead (mirrors
`pi-mono/.../openai-codex-responses.ts:326`).

Doing the lift in `_oauth_middleware.OpenAICodexOAuthMiddleware` is not
sufficient because `deepagents` SDK middlewares (`MemoryMiddleware`,
`SkillsMiddleware`, `FilesystemMiddleware`, …) re-populate
`request.system_message` via `append_to_system_message` AFTER our
middleware runs, regardless of position in the chain. By the time the
agent factory's `_execute_model_sync` calls `model_.invoke(messages)`,
a `SystemMessage` is back at index 0.

This subclass intercepts `_get_request_payload` — the single chokepoint
through which every code path flows (`_generate`, `_stream`,
`_stream_responses`, async variants) — strips `SystemMessage` instances
out of the message list, joins their text, and routes it through
`extra_body.instructions` so it lands as the top-level `instructions`
field on the wire.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

if TYPE_CHECKING:
    from langchain_core.language_models import LanguageModelInput


class _CodexChatOpenAI(ChatOpenAI):
    """ChatOpenAI variant that lifts SystemMessages to `instructions`.

    Behavioral diff from the parent:

    - `_get_request_payload` filters every `SystemMessage` out of the
      message list and joins their `.text` (separated by blank lines) into
      a single `instructions` string passed via `extra_body`.
    - When no SystemMessages are present, behaves identically to
      `ChatOpenAI`.
    """

    def _get_request_payload(  # type: ignore[override]
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        messages = self._convert_input(input_).to_messages()
        system_texts: list[str] = []
        rest = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                text = msg.text or ""
                if text:
                    system_texts.append(text)
            else:
                rest.append(msg)

        if system_texts:
            existing_extra = kwargs.get("extra_body")
            if existing_extra is None:
                existing_extra = self.extra_body or {}
            merged_extra = dict(existing_extra)
            instructions = "\n\n".join(system_texts)
            existing_instructions = merged_extra.get("instructions")
            if existing_instructions:
                merged_extra["instructions"] = (
                    f"{existing_instructions}\n\n{instructions}"
                )
            else:
                merged_extra["instructions"] = instructions
            kwargs["extra_body"] = merged_extra

        return super()._get_request_payload(rest, stop=stop, **kwargs)
