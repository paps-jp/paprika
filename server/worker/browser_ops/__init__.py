"""browser_ops package (split from the 3209-line module). Verbatim AST
slices into concern sub-modules; this __init__ re-exports every public
name so `import browser_ops; browser_ops.X(...)` is unchanged."""

from ._base import (  # noqa: F401
    ACTION_SETTLE_S,
    INCLUDE_BODY_TEXT,
    LogFn,
    MAX_AX_TREE_CHARS,
    MAX_OUTLINE_ITEMS,
    NAVIGATION_SETTLE_S,
    Snapshot,
    TAB_HOOKS_ENABLED,
    _BRACKET_ID_RE,
    canon_url,
    href_in_visited,
    normalize_selector,
    short_error,
)
from .dom import (  # noqa: F401
    _OUTLINE_JS,
    outline,
)
from .input import (  # noqa: F401
    _MODIFIER_BITS,
    _SPECIAL_KEY_CODES,
    _parse_key_combo,
    _resolve_key_payload,
    click,
    fill,
    press_key,
    scroll,
    type_text,
)
from .mouse import (  # noqa: F401
    _CURSOR_CLICK_JS,
    _CURSOR_HIDE_JS,
    _CURSOR_INJECT_JS,
    _CURSOR_MOVE_JS,
    _HUMAN_MOUSE_ENABLED,
    _MOUSE_DURATION_MS,
    _MOUSE_STEPS,
    _SHOW_CURSOR,
    _bezier_curve,
    _cursor_injected,
    _ease_in_out,
    _ensure_cursor,
    _flash_click,
    _human_move_to,
    _last_mouse,
    _mouse_button,
    _move_cursor,
    click_at,
    hover_at,
    type_at,
    wheel_at,
)
from .nav import (  # noqa: F401
    _entry_url,
    back,
    exists,
    forward,
    history_first,
    navigate,
    wait_for_load,
)
from .response import (  # noqa: F401
    _VAR_PLACEHOLDER_RE,
    _apply_variables,
    _capture_nav_response,
    _request_event_url,
    _resource_type_is_document,
    execute,
    execute_nav_with_response,
    install_last_response_tracker,
)
from .capture import (  # noqa: F401
    SESSION_SAVE_MIME_PREFIXES,
    _SESSION_EXT_TO_MIME,
    _session_effective_mime,
    _session_filename,
    _session_unique_path,
    capture,
    install_session_asset_capture,
    safe_label,
)
from .media import (  # noqa: F401
    _AUTOPLAY_ALL_FRAMES,
    _AUTOPLAY_CLICK_JS,
    _AUTOPLAY_ENABLED,
    _URL_CAPTURE_HOOK_JS,
    force_single_tab,
    install_iframe_deep_trace,
    install_url_capture_hook,
    read_url_capture,
    trigger_autoplay,
    trigger_autoplay_trusted,
)
