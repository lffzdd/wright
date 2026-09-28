"""Fullscreen layout applied by WrightTUI."""

APP_CSS = """
Screen {
    background: #14181d;
    color: #dce3eb;
}

#status {
    dock: top;
    height: 3;
    background: #191f27;
    color: #9aa9ba;
    padding: 1 2;
}

#brand {
    width: auto;
    color: #a9c7ee;
    text-style: bold;
}

#status-model {
    width: auto;
    padding: 0 1;
    color: #bfa8e6;
}

#status-workspace {
    width: auto;
    padding: 0 1;
    color: #8da4be;
}

#status-state {
    width: auto;
    padding: 0 1;
    color: #94b9ae;
}

#status-state.running {
    color: #d7bb82;
    text-style: bold;
}

#status-meta {
    width: 1fr;
    color: #9aa9ba;
    text-align: right;
}

#context {
    width: auto;
    padding-left: 2;
    color: #94c9b2;
}

#context.warning {
    color: #c4a35a;
}

#transcript {
    height: 1fr;
    padding: 1 3;
    scrollbar-size: 1 1;
    scrollbar-background: #14181d;
    scrollbar-color: #354456;
    scrollbar-color-hover: #6682a1;
}

#composer-wrap {
    dock: bottom;
    height: auto;
    background: #14181d;
    padding: 0 2 1 2;
}

#attachments {
    display: none;
    height: auto;
    margin: 0 1;
    padding: 0 1;
    color: #b9c8d8;
    background: #202a35;
    border-left: solid #52718f;
}

#composer {
    height: 5;
    min-height: 5;
    max-height: 8;
    background: #191f27;
    border: round #354456;
    padding: 0 1;
}

#composer:focus {
    border: round #83a9d4;
}

#composer-hint {
    height: 1;
    padding: 0 2;
    color: #8091a5;
}

#slash-suggestions {
    display: none;
    height: auto;
    max-height: 8;
    margin: 0 1;
    padding: 0 1;
    background: #202a35;
    border: round #52718f;
    color: #b9c8d8;
}

.msg {
    height: auto;
    margin: 0 0 1 0;
    padding: 0 0 0 1;
}

.user {
    color: #c3d8f2;
    background: #1c2734;
    border-left: thick #83a9d4;
    padding: 1 2;
}

.assistant {
    color: #dce3eb;
    border-left: solid #527767;
    padding: 0 2;
}

.assistant.draft {
    color: #bdcbdc;
    border-left: solid #6682a1;
}

.reasoning {
    height: auto;
    margin: 0 0 1 1;
    color: #a4afc2;
    background: #191f27;
    border: none;
    border-left: solid #696b88;
    padding: 0;
}

.reasoning-body {
    color: #a4afc2;
    padding: 0 1 1 1;
}

.subagent {
    height: auto;
    margin: 0 0 1 1;
    background: #171d26;
    border-left: solid #7c6f9e;
    padding: 0 1;
}

.subagent.running {
    border-left: solid #68a0cf;
}

.subagent.completed {
    border-left: solid #5da984;
}

.subagent.failed {
    border-left: solid #cf6868;
}

.system {
    color: #94a2b3;
    border-left: solid #354456;
    text-style: italic;
}

.usage {
    height: auto;
    margin: 0 0 1 2;
    color: #8999ad;
}

.usage.total {
    color: #a1bbd8;
    width: auto;
    padding: 0 1;
    background: #202c39;
    margin: 0 0 1 2;
}

.usage.total:hover {
    background: #2b3c4e;
    color: #dce3eb;
}

.tool {
    height: auto;
    margin: 0 0 1 1;
    background: #191f27;
    border: none;
    border-left: solid #354456;
    padding: 0;
}

.tool.running {
    color: #d7bb82;
}

.tool.done {
    color: #94c9b2;
}

.tool.error {
    color: #e49b9b;
}

.tool-body {
    color: #b0bdcc;
    padding: 0 1 1 1;
}

ModalScreen {
    align: center middle;
}

#dialog {
    width: 72;
    max-width: 95%;
    height: auto;
    max-height: 80%;
    background: #191f27;
    border: round #83a9d4;
    padding: 1 2;
}

.dialog-kicker {
    color: #8aa0b8;
    text-style: italic;
}

.dialog-title {
    text-style: bold;
    color: #f0f0f0;
    margin: 1 0;
}

.dialog-subject {
    color: #c8c8c8;
    margin-bottom: 1;
}

.dialog-meta {
    color: #c4a35a;
}

.dialog-reason {
    color: #9a9a9a;
    margin: 1 0;
}

.dialog-actions {
    height: auto;
    margin-top: 1;
}

.dialog-options {
    height: auto;
    margin: 1 0;
}

Tooltip {
    background: #253241;
    color: #e0e8f2;
    border: round #6682a1;
    padding: 1 2;
    max-width: 60;
}
"""
