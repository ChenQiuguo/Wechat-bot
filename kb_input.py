# -*- coding: utf-8 -*-
"""不用剪贴板往微信输入框打字。

为什么需要它：上游库发送消息走「写剪贴板 → Ctrl+V」（UIA 快路径的
``_paste_into`` 和 OCR 回退路径的 ``input_text`` 都是），而用户开了剪贴板
多端同步——每次发消息都会把回复内容同步到手机/其它电脑。

做法：``SendInput`` + ``KEYEVENTF_UNICODE`` 逐字注入 UTF-16 编码单元，
完全绕开剪贴板。本机实测微信 4.1.9.35 的 ``chat_input_field`` 接受这种方式
（中英文、标点、代理对 emoji 都进得去），且剪贴板前后内容一字未变。

风险与兜底：注入**是否真的落进输入框**必须校验（``get_value()`` 比对）。
校验不过就清空输入框并返回 False，由调用方回退到剪贴板路径——绝不带着
「可能写了一半」的输入框去按回车。
"""
from __future__ import annotations

import ctypes
import time

try:
    import win32con  # pywin32：用于剪贴板备份/恢复（可选）
except Exception:  # noqa: BLE001
    win32con = None

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---- SendInput 结构（INPUT / KEYBDINPUT）----
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
VK_DELETE = 0x2E
VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion)]


def _send(inp) -> int:
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _key_vk(vk: int, up: bool = False) -> int:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=0,
                          dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
    return _send(inp)


def tap_vk(vk: int, hold: float = 0.012):
    """按一下某个虚拟键（如 Delete / Return）。"""
    _key_vk(vk)
    time.sleep(hold)
    _key_vk(vk, up=True)


def _char(code_unit: int, up: bool = False) -> int:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=0, wScan=code_unit,
                          dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                          time=0, dwExtraInfo=0)
    return _send(inp)


def type_unicode(text: str, per_char_sleep: float = 0.012) -> tuple:
    """逐字注入文本。返回 (落空字符数, 实际注入了几个编码单元)。

    非 BMP 字符（emoji）按 UTF-16 代理对拆成两次注入。
    """
    bad = 0
    units = 0
    for ch in text:
        cp = ord(ch)
        if cp > 0xFFFF:
            pair = [0xD800 + ((cp - 0x10000) >> 10), 0xDC00 + ((cp - 0x10000) & 0x3FF)]
        else:
            pair = [cp]
        for u in pair:
            if _char(u) == 0:
                bad += 1
            _char(u, up=True)
            units += 1
        time.sleep(per_char_sleep)
    return bad, units


# --------------------------------------------------------------------------
# 与输入框控件交互
# --------------------------------------------------------------------------

def get_value(ctrl) -> str:
    """读输入框当前内容；读不到返回 None（区分「空」和「读不到」）。"""
    for getter in ("GetValuePattern",):
        try:
            vp = getattr(ctrl, getter)()
            if vp is not None and not vp.IsReadOnly:
                return vp.Value or ""
        except Exception:  # noqa: BLE001
            pass
    try:
        return ctrl.GetTextPattern().DocumentRange.GetText(-1) or ""
    except Exception:  # noqa: BLE001
        return None


def clear(ctrl) -> bool:
    """选中全部并删除，返回是否确认清空。"""
    try:
        ctrl.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
    except Exception:  # noqa: BLE001
        _key_vk(VK_CONTROL)
        tap_vk(VK_A)
        _key_vk(VK_CONTROL, up=True)
        tap_vk(VK_DELETE)
    time.sleep(0.15)
    val = get_value(ctrl)
    return val == "" if val is not None else True


def type_into(ctrl, text: str, per_char_sleep: float = 0.012) -> bool:
    """把 text 打进 ctrl：先清空再注入，最后**校验内容一致**。

    校验不过就再清空一次并返回 False（调用方应回退到剪贴板方案），
    保证不会带着残缺内容去按回车发送。
    """
    global type_attempts
    type_attempts += 1
    if not clear(ctrl):
        return False
    bad, units = type_unicode(text, per_char_sleep)
    time.sleep(0.15 + units * 0.002)
    val = get_value(ctrl)
    if val == text:
        return True
    # 允许「实际输入比目标多/少空白」这类无关紧要的差异
    if val is not None and val.strip() == text.strip() and text.strip():
        return True
    clear(ctrl)
    return False


# --------------------------------------------------------------------------
# 剪贴板备份 / 恢复（回退路径用：粘完把用户原来的剪贴板放回去）
# --------------------------------------------------------------------------

def clipboard_snapshot():
    """尽量把剪贴板里能还原的格式都存下来（文本 + 图片等），失败返回 None。"""
    if win32con is None:
        return None
    try:
        import win32clipboard as cb
        import win32gui
    except Exception:  # noqa: BLE001
        return None
    saved = []
    try:
        cb.OpenClipboard(None)
        try:
            fmt = 0
            while True:
                fmt = cb.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                try:
                    data = cb.GetClipboardData(fmt)
                except Exception:  # noqa: BLE001
                    continue
                saved.append((fmt, data))
        finally:
            cb.CloseClipboard()
    except Exception:  # noqa: BLE001
        return None
    return saved


def clipboard_restore(saved) -> bool:
    """把 clipboard_snapshot 的结果写回剪贴板。"""
    if not saved or win32con is None:
        return False
    try:
        import win32clipboard as cb
        cb.OpenClipboard(None)
        try:
            cb.EmptyClipboard()
            for fmt, data in saved:
                try:
                    cb.SetClipboardData(fmt, data)
                except Exception:  # noqa: BLE001
                    continue
        finally:
            cb.CloseClipboard()
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 开关：由 wx_bot 启动时按 config 设置
# --------------------------------------------------------------------------

_USE_UNICODE = True
type_attempts = 0      # 真正尝试过 Unicode 注入的次数（补丁用它判断「是否该回退剪贴板」）


def set_unicode_typing(on: bool):
    global _USE_UNICODE
    _USE_UNICODE = bool(on)


def unicode_typing_enabled() -> bool:
    return _USE_UNICODE
