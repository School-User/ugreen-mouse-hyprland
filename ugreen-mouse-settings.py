#!/usr/bin/env python3
"""UGREEN Mouse settings for Hyprland (laptop-side, this mouse only).

The receiver (2b89:0043) speaks a vendor protocol no UGREEN software supports, so
like KM Center does for this model, everything here is applied by the compositor:
per-device libinput settings (hl.device) and button remaps restricted to this
mouse (hl.bind ... device = { list = { DEVICE } }), including click / hold /
hold + wheel actions.

  ugreen-mouse-settings              open the settings window
  ugreen-mouse-settings --apply      regenerate the Hyprland config from saved settings
  ugreen-mouse-settings --selftest   check config generation
"""
import json, os, subprocess, sys, tempfile

DEVICE = 'ugreen-receiver--mouse'
SETTINGS_PATH = os.path.expanduser('~/.config/ugreen-mouse/settings.json')
LUA_PATH = os.path.expanduser('~/.config/hypr/ugreen_mouse.lua')
REQUIRE_LINE = 'require("hypr.ugreen_mouse")'

ALL_SLOTS = ('tap', 'hold', 'wheel_up', 'wheel_down')
SLOT_LABELS = {'tap': 'Click', 'hold': 'Hold', 'wheel_up': 'Hold + wheel up', 'wheel_down': 'Hold + wheel down'}
LUA_SLOT = {'tap': 'tap', 'hold': 'hold', 'wheel_up': 'up', 'wheel_down': 'down'}
BUTTONS = [('middle', 'mouse:274', 'Middle button', ('tap', 'hold')),
           ('back', 'mouse:275', 'Back side button', ALL_SLOTS),
           ('forward', 'mouse:276', 'Forward side button', ALL_SLOTS)]


def _exec(cmd):
    return f'hl.dsp.exec_cmd("{cmd}")'


# action id -> (label, Lua dispatcher). 'default' = the button's own click for
# "Click", nothing for the other slots; 'custom' uses the slot's command.
ACTIONS = {
    'default': ('Default', None),
    'copy': ('Copy', 'clip("CTRL", "C", "CTRL", "Insert")'),
    'paste': ('Paste', 'clip("CTRL", "V", "SHIFT", "Insert")'),
    'ws_prev': ('Previous workspace', 'hl.dsp.focus({ workspace = "e-1" })'),
    'ws_next': ('Next workspace', 'hl.dsp.focus({ workspace = "e+1" })'),
    'close': ('Close window', 'hl.dsp.window.close()'),
    'vol_down': ('Volume down', _exec('omarchy-audio-output-volume lower')),
    'vol_up': ('Volume up', _exec('omarchy-audio-output-volume raise')),
    'mute': ('Mute', _exec('omarchy-audio-output-volume mute-toggle')),
    'screenshot': ('Screenshot', _exec('omarchy-capture-screenshot')),
    'custom': ('Custom command…', None),
}
ACTION_IDS = list(ACTIONS)
ACCELS = ['adaptive', 'flat']

# Hyprland's own defaults.
DEFAULTS = {
    'sensitivity': 0.0, 'accel_profile': 'adaptive', 'scroll_factor': 1.0,
    'natural_scroll': False, 'left_handed': False, 'hold_ms': 400,
}

CLIP_HELPER = '''-- Same trick as Omarchy's universal clipboard: terminals get CTRL/SHIFT+Insert.
local function clip(mods, key, term_mods, term_key)
  return function()
    local m, k = mods, key
    local window = hl.get_active_window()
    for _, tag in ipairs((window and window.tags) or {}) do
      if tag:gsub("%*$", "") == "terminal" then m, k = term_mods, term_key end
    end
    hl.dispatch(hl.dsp.send_key_state({ mods = m, key = k, state = "down" }))
    hl.timer(function()
      hl.dispatch(hl.dsp.send_key_state({ mods = m, key = k, state = "up" }))
    end, { timeout = 50, type = "oneshot" })
  end
end
'''

# Click / hold / hold+wheel engine. The wheel binds stay disabled except while a
# side button is held, so normal scrolling is never captured. Hyprland debounces
# wheel binds (binds:scroll_event_delay, 300 ms by default).
RUNTIME = '''local ONLY = { inclusive = true, list = { DEVICE } }

local function run(action)
  if type(action) == "function" then action() elseif action then hl.dispatch(action) end
end

-- Re-send the button's own click (e.g. back/forward) when it has extra actions.
local function native(key)
  return function()
    hl.dispatch(hl.dsp.send_key_state({ mods = "", key = key, state = "down" }))
    hl.timer(function()
      hl.dispatch(hl.dsp.send_key_state({ mods = "", key = key, state = "up" }))
    end, { timeout = 20, type = "oneshot" })
  end
end

local state = { active = nil, held = false, used = false }
local wheel, hold_timer, safety = {}, nil, nil

local function set_wheel(on)
  for _, bind in ipairs(wheel) do bind:set_enabled(on) end
end

local function make_wheel_binds()
  if #wheel > 0 then return end
  for _, dir in ipairs({ "up", "down" }) do
    local bind = hl.bind("mouse_" .. dir, function()
      local btn = state.active
      if btn and btn[dir] then
        state.used = true
        run(btn[dir])
      end
    end, { device = ONLY, description = "UGREEN mouse: wheel while holding a side button" })
    bind:set_enabled(false)
    table.insert(wheel, bind)
  end
end

local function tap_hold(key, label, btn)
  if btn.up or btn.down then make_wheel_binds() end
  hl.bind(key, function()
    state.active, state.held, state.used = btn, false, false
    if btn.up or btn.down then
      set_wheel(true)
      if safety then safety:set_enabled(false) end
      -- never leave the wheel captured if a release gets lost
      safety = hl.timer(function() set_wheel(false) end, { timeout = 15000, type = "oneshot" })
    end
    if hold_timer then hold_timer:set_enabled(false) end
    hold_timer = nil
    if btn.hold then
      hold_timer = hl.timer(function()
        if state.active == btn and not state.used then
          state.held = true
          run(btn.hold)
        end
      end, { timeout = HOLD_MS, type = "oneshot" })
    end
  end, { device = ONLY, description = "UGREEN mouse " .. label .. ": press" })
  hl.bind(key, function()
    set_wheel(false)
    if hold_timer then hold_timer:set_enabled(false) end
    if state.active == btn and not state.held and not state.used then run(btn.tap) end
    state.active = nil
  end, { release = true, device = ONLY, description = "UGREEN mouse " .. label .. ": release" })
end
'''


def _slot(label, raw):
    action, cmd = raw.get('action', 'default'), str(raw.get('command', '')).strip()
    if action not in ACTIONS:
        raise ValueError(f'{label}: unknown action {action!r}')
    if action == 'custom' and (not cmd or any(c in cmd for c in '\r\n\0')):
        raise ValueError(f'{label}: custom command must be a single non-empty line')
    return {'action': action, 'command': cmd}


def validate(raw):
    """Return a clean copy of raw settings, or raise ValueError."""
    s = {**DEFAULTS, **{k: v for k, v in raw.items() if k != 'buttons'}}
    sens, scroll, hold_ms = float(s['sensitivity']), float(s['scroll_factor']), int(s['hold_ms'])
    if not -1.0 <= sens <= 1.0:
        raise ValueError('Pointer speed must be between -1 and 1')
    if not 0.1 <= scroll <= 5.0:
        raise ValueError('Scroll speed must be between 0.1 and 5')
    if not 150 <= hold_ms <= 2000:
        raise ValueError('Hold delay must be between 150 and 2000 ms')
    if s['accel_profile'] not in ACCELS:
        raise ValueError(f'Unknown acceleration profile {s["accel_profile"]!r}')
    buttons = {}
    for b, _, label, slots in BUTTONS:
        rb = raw.get('buttons', {}).get(b, {})
        if 'action' in rb:  # 1.0 format: a single click action
            rb = {'tap': rb}
        buttons[b] = {slot: _slot(f'{label} {SLOT_LABELS[slot].lower()}', rb.get(slot, {})) for slot in slots}
    return {'sensitivity': round(sens, 2), 'accel_profile': s['accel_profile'],
            'scroll_factor': round(scroll, 2), 'natural_scroll': bool(s['natural_scroll']),
            'left_handed': bool(s['left_handed']), 'hold_ms': hold_ms, 'buttons': buttons}


def lua_str(text):
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def lua_bool(v):
    return 'true' if v else 'false'


def dispatcher(slot, cfg, key):
    """Lua expression for one slot, or None when the slot does nothing."""
    if cfg['action'] == 'custom':
        return f'hl.dsp.exec_cmd({lua_str(cfg["command"])})'
    if cfg['action'] == 'default':
        return f'native("{key}")' if slot == 'tap' else None
    return ACTIONS[cfg['action']][1]


def generate_lua(s):
    lines = [
        '-- Generated by ugreen-mouse-settings - edits here are overwritten.',
        f'local DEVICE = {lua_str(DEVICE)}',
        f'local HOLD_MS = {s["hold_ms"]}', '',
        f'hl.device({{ name = DEVICE, sensitivity = {s["sensitivity"]:.2f}, '
        f'accel_profile = {lua_str(s["accel_profile"])}, scroll_factor = {s["scroll_factor"]:.2f}, '
        f'natural_scroll = {lua_bool(s["natural_scroll"])}, left_handed = {lua_bool(s["left_handed"])} }})',
        '', CLIP_HELPER, RUNTIME,
    ]
    for b, key, label, slots in BUTTONS:
        cfg = s['buttons'][b]
        extras = [slot for slot in slots if slot != 'tap' and cfg[slot]['action'] != 'default']
        if extras:
            fields = [f'{LUA_SLOT[slot]} = {dsp}' for slot in slots
                      if (dsp := dispatcher(slot, cfg[slot], key))]
            lines.append(f'tap_hold("{key}", {lua_str(label)}, {{ {", ".join(fields)} }})')
        elif cfg['tap']['action'] != 'default':
            desc = lua_str(f'UGREEN mouse {label}: {ACTIONS[cfg["tap"]["action"]][0]}')
            lines.append(f'hl.bind("{key}", {dispatcher("tap", cfg["tap"], key)}, '
                         f'{{ device = ONLY, description = {desc} }})')
    return '\n'.join(lines) + '\n'


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return validate({})


def write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def apply(settings):
    """Save settings, regenerate the Lua module, reload Hyprland. Returns an error string or None."""
    s = validate(settings)
    with open(os.path.expanduser('~/.config/hypr/hyprland.lua')) as f:
        if REQUIRE_LINE not in f.read():
            return f'~/.config/hypr/hyprland.lua is missing {REQUIRE_LINE}'
    write_atomic(SETTINGS_PATH, json.dumps(s, indent=2) + '\n')
    write_atomic(LUA_PATH, generate_lua(s))
    subprocess.run(['hyprctl', 'reload'], capture_output=True, check=True)
    errors = subprocess.run(['hyprctl', 'configerrors'], capture_output=True, text=True).stdout.strip()
    return errors or None


def mouse_connected():
    try:
        out = subprocess.run(['hyprctl', 'devices', '-j'], capture_output=True, text=True, check=True).stdout
        return any(m['name'] == DEVICE for m in json.loads(out)['mice'])
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        return False


def selftest():
    # 1.0-format settings still load and give plain binds
    s = validate({'sensitivity': 0.3, 'accel_profile': 'flat',
                  'buttons': {'back': {'action': 'copy'},
                              'forward': {'action': 'custom', 'command': 'notify-send "hi \\o/"'}}})
    lua = generate_lua(s)
    assert 'sensitivity = 0.30, accel_profile = "flat"' in lua
    assert 'hl.bind("mouse:275", clip("CTRL", "C", "CTRL", "Insert")' in lua
    assert 'hl.dsp.exec_cmd("notify-send \\"hi \\\\o/\\"")' in lua
    assert 'mouse:274' not in lua and 'tap_hold("' not in lua  # defaults = no binds
    # click/hold/wheel slots go through tap_hold; default click re-sends the button
    s = validate({'hold_ms': 500, 'buttons': {
        'back': {'hold': {'action': 'screenshot'}, 'wheel_up': {'action': 'vol_up'}},
        'middle': {'tap': {'action': 'close'}, 'hold': {'action': 'custom', 'command': 'foo'}}}})
    lua = generate_lua(s)
    assert 'local HOLD_MS = 500' in lua
    assert ('tap_hold("mouse:275", "Back side button", { tap = native("mouse:275"), '
            'hold = hl.dsp.exec_cmd("omarchy-capture-screenshot"), '
            'up = hl.dsp.exec_cmd("omarchy-audio-output-volume raise") })') in lua
    assert ('tap_hold("mouse:274", "Middle button", { tap = hl.dsp.window.close(), '
            'hold = hl.dsp.exec_cmd("foo") })') in lua
    assert 'mouse:276' not in lua.split(RUNTIME)[1]
    for bad in ({'sensitivity': 2}, {'scroll_factor': 0}, {'accel_profile': 'warp'}, {'hold_ms': 50},
                {'buttons': {'back': {'action': 'custom', 'command': 'a\nb'}}},
                {'buttons': {'back': {'hold': {'action': 'custom', 'command': ''}}}},
                {'buttons': {'middle': {'tap': {'action': 'nope'}}}}):
        try:
            validate(bad)
            raise AssertionError(f'accepted {bad}')
        except ValueError:
            pass
    print('selftest ok')


def run_app():
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gtk

    def scale_row(title, subtitle, lo, hi, step, digits=2):
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_digits(digits)
        scale.set_draw_value(True)
        scale.set_size_request(220, -1)
        scale.set_valign(Gtk.Align.CENTER)
        row.add_suffix(scale)
        return row, scale

    def build(app):
        win = Adw.ApplicationWindow(application=app, title='UGREEN Mouse', default_width=480, default_height=720)
        toasts = Adw.ToastOverlay()
        page = Adw.PreferencesPage()
        toasts.set_child(page)
        header = Adw.HeaderBar()
        apply_btn = Gtk.Button(label='Apply', css_classes=['suggested-action'])
        reset_btn = Gtk.Button(icon_name='edit-undo-symbolic', tooltip_text='Reset to defaults')
        header.pack_end(apply_btn)
        header.pack_start(reset_btn)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(toasts)
        win.set_content(view)

        dev = Adw.PreferencesGroup()
        dev.add(Adw.ActionRow(title='UGREEN Receiver', icon_name='input-mouse-symbolic',
                              subtitle='Connected' if mouse_connected() else 'Not connected - settings apply when it is'))
        page.add(dev)

        pointer = Adw.PreferencesGroup(title='Pointer')
        speed_row, speed = scale_row('Speed', 'Slow to fast', -1.0, 1.0, 0.05)
        accel = Adw.ComboRow(title='Acceleration', model=Gtk.StringList.new(['Adaptive', 'Flat (raw)']))
        pointer.add(speed_row)
        pointer.add(accel)
        page.add(pointer)

        scrolling = Adw.PreferencesGroup(title='Scrolling')
        scroll_row, scroll = scale_row('Scroll speed', 'Multiplier', 0.1, 5.0, 0.1)
        natural = Adw.SwitchRow(title='Natural scrolling', subtitle='Content follows the wheel')
        scrolling.add(scroll_row)
        scrolling.add(natural)
        page.add(scrolling)

        buttons = Adw.PreferencesGroup(title='Buttons', description='Remaps apply to this mouse only')
        lefty = Adw.SwitchRow(title='Left-handed', subtitle='Swap left and right buttons')
        hold_row, hold_ms = scale_row('Hold delay', 'Milliseconds before a press counts as a hold',
                                      150, 1000, 50, digits=0)
        buttons.add(lefty)
        buttons.add(hold_row)
        click_labels = Gtk.StringList.new([ACTIONS[a][0] for a in ACTION_IDS])
        extra_labels = Gtk.StringList.new(['None'] + [ACTIONS[a][0] for a in ACTION_IDS[1:]])
        combos, entries = {}, {}
        for b, _, label, slots in BUTTONS:
            expander = Adw.ExpanderRow(title=label, subtitle=', '.join(SLOT_LABELS[s] for s in slots))
            for slot in slots:
                combo = Adw.ComboRow(title=SLOT_LABELS[slot],
                                     model=click_labels if slot == 'tap' else extra_labels)
                entry = Adw.EntryRow(title=f'{SLOT_LABELS[slot]} command')
                combo.connect('notify::selected',
                              lambda c, _p, e=entry: e.set_visible(ACTION_IDS[c.get_selected()] == 'custom'))
                expander.add_row(combo)
                expander.add_row(entry)
                combos[b, slot], entries[b, slot] = combo, entry
            buttons.add(expander)
        page.add(buttons)

        def show(s):
            speed.set_value(s['sensitivity'])
            accel.set_selected(ACCELS.index(s['accel_profile']))
            scroll.set_value(s['scroll_factor'])
            natural.set_active(s['natural_scroll'])
            lefty.set_active(s['left_handed'])
            hold_ms.set_value(s['hold_ms'])
            for (b, slot), combo in combos.items():
                cfg = s['buttons'][b][slot]
                combo.set_selected(ACTION_IDS.index(cfg['action']))
                entries[b, slot].set_text(cfg['command'])
                entries[b, slot].set_visible(cfg['action'] == 'custom')

        def collect():
            buttons_cfg = {b: {} for b, _, _, _ in BUTTONS}
            for (b, slot), combo in combos.items():
                buttons_cfg[b][slot] = {'action': ACTION_IDS[combo.get_selected()],
                                        'command': entries[b, slot].get_text()}
            return {'sensitivity': speed.get_value(), 'accel_profile': ACCELS[accel.get_selected()],
                    'scroll_factor': scroll.get_value(), 'natural_scroll': natural.get_active(),
                    'left_handed': lefty.get_active(), 'hold_ms': int(hold_ms.get_value()),
                    'buttons': buttons_cfg}

        def on_apply(_btn):
            try:
                err = apply(collect())
            except (ValueError, OSError, subprocess.CalledProcessError) as e:
                err = str(e)
            toasts.add_toast(Adw.Toast(title=f'Not applied: {err}' if err else 'Applied', timeout=4))

        apply_btn.connect('clicked', on_apply)
        reset_btn.connect('clicked', lambda _b: show(validate({})))
        try:
            show(load_settings())
        except (ValueError, OSError) as e:
            show(validate({}))
            toasts.add_toast(Adw.Toast(title=f'Saved settings ignored: {e}'))
        win.present()

    app = Adw.Application(application_id='io.github.SchoolUser.UgreenMouse')
    app.connect('activate', build)
    app.run([])


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    elif '--apply' in sys.argv:
        err = apply(load_settings())
        sys.exit(f'Not applied: {err}' if err else 0)
    else:
        run_app()
