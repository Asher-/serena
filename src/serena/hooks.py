import json
import os
import pickle
import shutil
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal, Self

import click

from serena.util.cli_util import AutoRegisteringGroup

# copied from serena_config.py, we don't want to import anything here to keep the hook commands fast
serena_home_dir = os.getenv("SERENA_HOME", "").strip() or str(Path.home() / ".serena")


class HookClient(Enum):
    """The client application that triggered the hook."""

    CLAUDE_CODE = "claude-code"
    VSCODE = "vscode"
    CODEX = "codex"


class Hook(ABC):
    def __init__(self, client: HookClient):
        raw = sys.stdin.read()
        input_data = json.loads(raw)
        self._input_data = input_data
        self._client = client

        session_id = input_data.get("session_id") or input_data.get("sessionId")
        if not session_id:
            raise ValueError("Session ID is required in the hook input data")
        self._session_id = str(session_id)
        self.session_persistence_dir = os.path.join(serena_home_dir, "hook_data", self._session_id)
        # tool input has a timestamp but using now is enough
        self.triggered_at_timestamp = datetime.now()

    @abstractmethod
    def execute(self) -> None:
        pass


class PreToolUseHook(Hook, ABC):
    def __init__(self, client: HookClient):
        super().__init__(client)
        _tool_name = self._input_data.get("tool_name") or self._input_data.get("toolName", "") or ""
        _tool_name = str(_tool_name).lower().strip()
        if not _tool_name:
            raise ValueError("Tool name is required in the hook input data")
        self._tool_name = _tool_name
        self._tool_input: dict | None = self._input_data.get("tool_input") or self._input_data.get("toolInput")

        # only relevant in claude code at the moment, (not all events include this field; default to empty string)
        raw_permission_mode = self._input_data.get("permission_mode") or self._input_data.get("permissionMode") or ""
        self._permission_mode = str(raw_permission_mode).strip()

    @dataclass
    class OutputData:
        permission_decision: Literal["deny", "allow"]
        permission_decision_reason: str
        additional_context: str = ""

        def to_json_string(self) -> str:
            hook_output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": self.permission_decision,
                    "permissionDecisionReason": self.permission_decision_reason,
                    "additionalContext": self.additional_context,
                }
            }
            return json.dumps(hook_output)

    def is_serena_tool(self) -> bool:
        return "serena" in self._tool_name


class PreToolUseRemindAboutSerenaHook(PreToolUseHook):
    """Pre-tool-use hook that nudges the agent toward Serena's symbolic tools.

    Tracks consecutive uses of grep-style tools via a persisted
    :class:`ToolUseCounter`. When the number of recent calls reaches the
    configured threshold, a deny response is emitted with a reminder to
    use symbolic alternatives.

    The counter is reset whenever

    * a Serena tool is invoked,
    * a deny is emitted (so the next retry starts fresh),
    * or the configured reset period elapses *between two consecutive grep
      calls* — i.e. the period gates the gap between successive calls, not
      an absolute sliding window. Three grep calls at t=0, t=9, t=18 therefore
      count as a burst of three, even though the total span (18s) exceeds the
      10s grep period; only an individual pair that is more than 10s apart
      resets the counter.

    Non-tracked tools (Edit, Write, Bash, Read, etc.) are deliberately neutral:
    they neither increment nor reset the counter, so they also do not mask
    bursts by pushing the last timestamp forward.

    The hook is additionally gated by :attr:`ToolUseCounter._MIN_DENY_INTERVAL_SECONDS`
    (two minutes by default): once a deny has been emitted, *every* subsequent
    invocation of this hook is a no-op until the window has elapsed — neither the
    counter is updated nor any further deny is produced. This prevents the agent
    from being nudged more than once per window during a sustained grep burst,
    and also avoids surprising the user with reminders that were already counted
    up under stale state.
    """

    @dataclass
    class ToolUseCounter:
        _FILE_NAME = "tool_use_counter.pkl"
        _GREP_USES_THRESHOLD = 3

        # The reset period is set to essentially infinity since we neglect it in practice
        _GREP_RESET_PERIOD_SECONDS = 1000

        # minimum seconds between two engagements of the hook after a deny; the entire
        # hook (counter updates included) is a no-op while this window is active, so a
        # single sustained burst triggers at most one nudge per window
        _MIN_DENY_INTERVAL_SECONDS = 120

        n_recent_grep_uses: int = 0
        last_grep_use_timestamp: datetime | None = None
        # timestamp of the most recently emitted deny; deliberately not cleared by
        # :meth:`reset` so the rate limit survives counter resets (e.g. Serena tool use)
        last_deny_timestamp: datetime | None = None

        def too_many_recent_greps(self) -> bool:
            return self.n_recent_grep_uses >= self._GREP_USES_THRESHOLD

        def is_hook_active(self, now: datetime) -> bool:
            """:return: whether the hook should engage at all at ``now``. Returns
            ``False`` while we are still within :attr:`_MIN_DENY_INTERVAL_SECONDS`
            of the most recent emitted deny — in that case the entire hook is
            short-circuited (no counter updates, no deny). Returns ``True`` when
            no deny has been emitted yet in this session, or when the window has
            elapsed.
            """
            if self.last_deny_timestamp is None:
                return True
            return (now - self.last_deny_timestamp).total_seconds() >= self._MIN_DENY_INTERVAL_SECONDS

        @classmethod
        def _get_persistence_path(cls, hook: Hook) -> Path:
            return Path(hook.session_persistence_dir) / cls._FILE_NAME

        @classmethod
        def load(cls, hook: Hook) -> Self:
            path = cls._get_persistence_path(hook)
            try:
                with open(path, "rb") as f:
                    return pickle.load(f)
            except Exception:
                return cls()

        def save(self, hook: Hook) -> None:
            path = self._get_persistence_path(hook)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "wb") as f:
                    pickle.dump(self, f)
            except Exception:
                pass

        def update(self, hook: "PreToolUseRemindAboutSerenaHook") -> None:
            if hook.is_serena_tool():
                self.reset()
                return

            if not hook.is_grep_tool():
                return

            now = hook.triggered_at_timestamp
            grep_period = self._GREP_RESET_PERIOD_SECONDS
            if self.last_grep_use_timestamp is not None and (now - self.last_grep_use_timestamp).total_seconds() <= grep_period:
                self.n_recent_grep_uses += 1
            else:
                self.n_recent_grep_uses = 1
            self.last_grep_use_timestamp = now

        def reset(self) -> None:
            self.n_recent_grep_uses = 0
            self.last_grep_use_timestamp = None

    def __init__(self, client: HookClient):
        super().__init__(client)
        self._tool_call_counter = self.ToolUseCounter.load(self)

    def is_grep_tool(self) -> bool:
        if self._client == HookClient.CLAUDE_CODE:
            return self._tool_name == "grep"
        return "grep" in self._tool_name

    def execute(self) -> None:
        # gate the entire hook on the rate-limit window: while we are within
        # _MIN_DENY_INTERVAL_SECONDS of the last emitted deny, no counter
        # updates and no deny detection happen at all. The pickle is left
        # untouched so state survives the gating window unchanged.
        if not self._tool_call_counter.is_hook_active(self.triggered_at_timestamp):
            return

        self._tool_call_counter.update(self)

        output_data: PreToolUseHook.OutputData | None = None
        if self._tool_call_counter.too_many_recent_greps():
            output_data = self._build_grep_deny()

        if output_data is not None:
            # reset burst counters so the next interval starts fresh, then record
            # the deny timestamp and emit. The is_hook_active() guard at the top
            # ensures we never reach this branch within the rate-limit window.
            self._tool_call_counter.reset()
            self._tool_call_counter.last_deny_timestamp = self.triggered_at_timestamp
            click.echo(output_data.to_json_string())
        self._tool_call_counter.save(self)

    def _build_grep_deny(self) -> "PreToolUseHook.OutputData":
        return self.OutputData(
            permission_decision="deny",
            permission_decision_reason="Too many consecutive grep calls without using symbolic tools. "
            "You can continue using grep now if needed, the counter was reset.",
            additional_context=(
                "You were using many grep calls recently. Consider using Serena's symbolic "
                "mcp tools instead for more code-centric search. You can continue using grep now if needed, the counter was reset."
            ),
        )


class SessionStartActivateProjectHook(Hook):
    def execute(self) -> None:
        message = (
            "**IMPORTANT**: Activate the current working directory as project using Serena's tools if not already done. "
            "If you haven't read Serena Instructions Manual yet, do so. "
            "Follow this instruction before doing anything else."
        )
        result = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": message,
            }
        }
        click.echo(json.dumps(result))


class SessionEndCleanupHook(Hook):
    def execute(self) -> None:
        shutil.rmtree(self.session_persistence_dir, ignore_errors=True)


class PreToolUseAutoApproveSerenaHook(PreToolUseHook):
    """Pre-tool-use hook that auto-approves Serena tool calls while the client is in accept-edits mode.

    Claude Code's ``acceptEdits`` permission mode only applies to its built-in editing tools;
    Serena's destructive tools (e.g. ``replace_symbol_body`` or ``rename_symbol``) would still
    prompt the user on every call. This hook emits an ``allow`` decision for any Serena MCP
    tool call whenever the client reports ``acceptEdits`` as the active permission mode, so
    blanket edit approvals also cover Serena's tools. In all other situations it stays silent,
    preserving the default approval flow.
    """

    _ACCEPT_EDITS_MODE = "acceptEdits"

    def is_accept_edits_mode(self) -> bool:
        return self._permission_mode == self._ACCEPT_EDITS_MODE

    def execute(self) -> None:
        # only emit a decision when both the tool and the mode match; stay silent otherwise
        if not self.is_serena_tool() or not self.is_accept_edits_mode():
            return

        output_data = self.OutputData(
            permission_decision="allow",
            permission_decision_reason="Auto-approved: Serena tool call while client is in acceptEdits mode.",
        )
        click.echo(output_data.to_json_string())


_client_option = click.option(
    "--client",
    type=click.Choice([e.value for e in HookClient], case_sensitive=False),
    default=HookClient.CLAUDE_CODE.value,
    show_default=True,
    help="The client application that triggered the hook.",
)


class HookCommands(AutoRegisteringGroup):
    def __init__(self) -> None:
        super().__init__(name="serena-hook", help="Commands that send reminders to agents when appropriate, to be used in hooks.")

    @staticmethod
    @click.command(
        "activate",
        help="Set this as hook at session startup to prompt the agent to activate the project at the start of the session and read Serena's instructions",
    )
    @_client_option
    def activate(client: str) -> None:
        SessionStartActivateProjectHook(HookClient(client)).execute()

    @staticmethod
    @click.command("cleanup", help="Set this as hook at session end all hook data for the current session")
    @_client_option
    def cleanup(client: str) -> None:
        SessionEndCleanupHook(HookClient(client)).execute()

    @staticmethod
    @click.command(
        "remind",
        help="Set this as hook at PreToolUse to remind the agent to use Serena's tools instead of overrelying on grep",
    )
    @_client_option
    def remind(client: str) -> None:
        PreToolUseRemindAboutSerenaHook(HookClient(client)).execute()

    @staticmethod
    @click.command(
        "auto-approve",
        help="Set this as hook at PreToolUse to auto-approve Serena tool calls while the client is in acceptEdits mode (Claude Code).",
    )
    @_client_option
    def auto_approve(client: str) -> None:
        PreToolUseAutoApproveSerenaHook(HookClient(client)).execute()


hook_commands = HookCommands()
