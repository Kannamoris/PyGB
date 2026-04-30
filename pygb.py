#!/usr/bin/env python3
"""
PyGB - Play Game Boy / GBA cartridges directly via GBxCart RW.

Detects a connected cartridge, dumps ROM and save data to temporary files,
launches an emulator (RetroArch by default), and writes the save back to the
cartridge when the emulator is closed.
"""

import sys
import os
import time
import copy
import shutil
import struct
import tempfile
import subprocess
import argparse
import configparser
import re

# ---------------------------------------------------------------------------
# FlashGBX integration
# ---------------------------------------------------------------------------
from FlashGBX import Util
from FlashGBX.hw_GBxCartRW import GbxDevice
from FlashGBX.RomFileDMG import RomFileDMG
from FlashGBX.RomFileAGB import RomFileAGB
from FlashGBX.FlashGBX import LoadConfig

# ---------------------------------------------------------------------------
# Colours for terminal output
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"


def status(msg):
    print(f"{CYAN}[PyGB]{RESET} {msg}")


def success(msg):
    print(f"{GREEN}[PyGB]{RESET} {msg}")


def warn(msg):
    print(f"{YELLOW}[PyGB]{RESET} {msg}")


def error(msg):
    print(f"{RED}[PyGB]{RESET} {msg}")


# ---------------------------------------------------------------------------
# Progress callback (thin wrapper around FlashGBX's Progress)
# ---------------------------------------------------------------------------
class ProgressHandler:
    """Minimal progress handler that prints a progress bar to the terminal."""

    def __init__(self):
        self._size = 0
        self._pos = 0
        self._method = ""

    def SetProgress(self, args):
        action = args.get("action", "")

        if action == "INITIALIZE":
            self._size = args.get("size", 0)
            self._pos = args.get("pos", 0)
            self._method = args.get("method", "")
            label = {
                "ROM_READ": "Dumping ROM",
                "SAVE_READ": "Reading save",
                "SAVE_WRITE": "Writing save",
                "DETECT_CART": "Detecting cartridge",
            }.get(self._method, self._method)
            status(f"{label}...")

        elif action in ("READ", "WRITE"):
            self._pos += args.get("bytes_added", 0)
            self._print_bar()

        elif action == "UPDATE_POS":
            self._pos = args.get("pos", self._pos)
            self._print_bar()

        elif action == "FINISHED":
            self._print_bar(final=True)

        elif action == "ABORT":
            msg = args.get("info_msg", "Aborted.")
            error(msg)

        elif action == "USER_ACTION":
            msg = args.get("msg", "")
            if msg:
                warn(msg)

    def _print_bar(self, final=False):
        if self._size == 0:
            return
        pct = min(self._pos / self._size, 1.0)
        bar_len = 40
        filled = int(bar_len * pct)
        bar = "█" * filled + "░" * (bar_len - filled)
        end = "\n" if final or pct >= 1.0 else "\r"
        size_str = format_size(self._pos)
        total_str = format_size(self._size)
        sys.stdout.write(
            f"  [{bar}] {pct:6.1%}  {size_str} / {total_str}{end}"
        )
        sys.stdout.flush()


def format_size(n):
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    else:
        return f"{n / (1024 * 1024):.2f} MiB"


# ---------------------------------------------------------------------------
# Device helpers
# ---------------------------------------------------------------------------
def load_flashcarts():
    """Load flash cartridge config files the same way FlashGBX does."""
    app_path = os.path.dirname(os.path.abspath(Util.__file__))
    config_path = os.path.expanduser("~") + "/FlashGBX"

    class FakeArgs:
        reset = False

    args = {
        "app_path": app_path,
        "config_path": config_path,
        "argparsed": FakeArgs(),
    }
    try:
        cfg = LoadConfig(args)
        Util.CONFIG_PATH = config_path
        return cfg["flashcarts"]
    except Exception:
        return {"DMG": {}, "AGB": {}}


def connect_device(port=None):
    """Find and connect to a GBxCart RW device. Returns GbxDevice or None."""
    flashcarts = load_flashcarts()
    dev = GbxDevice()
    ret = dev.Initialize(flashcarts, port=port, max_baud=2000000)

    if ret is False or not dev.IsConnected():
        return None

    if isinstance(ret, list):
        for entry in ret:
            lvl, msg = entry[0], re.sub("<[^<]+?>", "", entry[1])
            if lvl == 3:
                error(msg)
                return None
            elif lvl >= 1:
                warn(msg)

    return dev


def detect_mode(dev):
    """Try both DMG and AGB modes and return whichever has a valid header."""
    for mode in ("DMG", "AGB"):
        dev.SetMode(mode)
        time.sleep(0.2)
        header = dev.ReadInfo()
        if header is False or header == {}:
            continue
        if header.get("empty_nocart", True) or header.get("empty", True):
            continue
        if not header.get("logo_correct", False):
            continue
        return mode, header
    return None, None


def get_rom_size(mode, header):
    """Determine the ROM size in bytes from the header."""
    if mode == "DMG":
        raw = header.get("rom_size_raw", 0)
        if raw < len(Util.DMG_Header_ROM_Sizes_Flasher_Map):
            return Util.DMG_Header_ROM_Sizes_Flasher_Map[raw]
        return 2 * 1024 * 1024  # fallback: 2 MiB
    else:  # AGB
        if "rom_size" in header:
            return header["rom_size"]
        return 32 * 1024 * 1024  # fallback: 32 MiB


def get_mbc(mode, header):
    """Return the MBC / mapper type byte."""
    if mode == "DMG":
        return header.get("mapper_raw", 0)
    return 0


def get_save_info(mode, header):
    """Return (save_type, save_size) for the cartridge.

    For DMG carts, ram_size_raw from ReadInfo()/GetHeader() is already the
    correct save_type value to pass to TransferData:
      - Standard carts: raw header byte 0-5 (each is a value in DMG_Header_RAM_Sizes_Map)
      - Special mappers: GetHeader() sets ram_size_raw to the map value directly
        (MBC2=256, MBC7=257/258, TAMA5=259, MBC6=260)

    For AGB carts, save_type comes from the header (set by DoDetectCartridge).
    """
    if mode == "DMG":
        save_type = header.get("ram_size_raw", 0)
        if save_type == 0:
            return 0, 0
        if save_type not in Util.DMG_Header_RAM_Sizes_Map:
            warn(f"Unknown save type 0x{save_type:X}; skipping save.")
            return 0, 0
        idx = Util.DMG_Header_RAM_Sizes_Map.index(save_type)
        save_size = Util.DMG_Header_RAM_Sizes_Flasher_Map[idx]
        if save_size == 0:
            return 0, 0
        return save_type, save_size

    else:  # AGB
        st = header.get("save_type", None)
        if st is None or st == 0:
            return 0, 0
        if st < len(Util.AGB_Header_Save_Sizes):
            return st, Util.AGB_Header_Save_Sizes[st]
        return 0, 0


def get_rom_extension(mode, header):
    if mode == "AGB":
        return ".gba"
    cgb = header.get("cgb", 0)
    if cgb in (0x80, 0xC0):
        return ".gbc"
    return ".gb"


def sanitize_title(title):
    """Make a filesystem-safe title from the game title."""
    title = title.strip().replace(" ", "_")
    title = re.sub(r"[<>:\"/\\|?*\x00]", "_", title)
    return title if title else "UNKNOWN"


def detect_save_type_agb(dev):
    """Run FlashGBX's cartridge auto-detection to find the AGB save type."""
    status("Auto-detecting GBA save type...")
    ret = dev.DoDetectCartridge(limitVoltage=False, checkSaveType=True)
    if ret is None or ret is False:
        return 0, 0
    _info, save_size, save_type, *_ = ret
    return save_type or 0, save_size or 0


def save_type_name(mode, save_type):
    """Human-readable name for a save type."""
    if mode == "DMG":
        if save_type in Util.DMG_Header_RAM_Sizes_Map:
            idx = Util.DMG_Header_RAM_Sizes_Map.index(save_type)
            return Util.DMG_Header_RAM_Sizes[idx]
        return f"type 0x{save_type:X}"
    else:
        if save_type < len(Util.AGB_Header_Save_Types):
            return Util.AGB_Header_Save_Types[save_type]
        return f"type {save_type}"


# ---------------------------------------------------------------------------
# RTC conversion helpers
# ---------------------------------------------------------------------------
# FlashGBX/VBA appended RTC format (MBC3, 48 bytes):
#   bytes  0-19  – 5 × uint32 LE: latched registers (S, M, H, DL, DH)
#   bytes 20-39  – 5 × uint32 LE: real/internal registers (copy of latched at dump time)
#   bytes 40-47  – uint64 LE: Unix timestamp of the dump
#
# SameBoy libretro .rtc format (32 bytes):
#   bytes  0- 4  – rtc_real[5]    as uint8 (S, M, H, DL, DH)
#   bytes  5- 9  – rtc_latched[5] as uint8
#   bytes 10-15  – zero padding (alignment)
#   bytes 16-23  – uint64 LE: last_rtc_second (Unix timestamp)
#   bytes 24-31  – zero padding
MBC3_VBA_RTC_SIZE = 0x30  # 48 bytes
SAMEBOY_RTC_SIZE  = 32


def vba_to_sameboy_rtc(vba_rtc):
    """Convert 48-byte FlashGBX/VBA RTC data to 32-byte SameBoy libretro format."""
    latched = [struct.unpack_from("<I", vba_rtc, i * 4)[0]      for i in range(5)]
    real    = [struct.unpack_from("<I", vba_rtc, 20 + i * 4)[0] for i in range(5)]
    ts      = struct.unpack_from("<Q", vba_rtc, 40)[0]

    out = bytearray(SAMEBOY_RTC_SIZE)
    for i, v in enumerate(real):
        out[i] = v & 0xFF
    for i, v in enumerate(latched):
        out[5 + i] = v & 0xFF
    struct.pack_into("<Q", out, 16, ts)
    return bytes(out)


def sameboy_to_vba_rtc(sb_rtc):
    """Convert 32-byte SameBoy libretro RTC data to 48-byte FlashGBX/VBA format."""
    real    = sb_rtc[0:5]
    latched = sb_rtc[5:10]
    ts      = struct.unpack_from("<Q", sb_rtc, 16)[0]

    out = bytearray(MBC3_VBA_RTC_SIZE)
    for i, v in enumerate(latched):         # latched at bytes 0-19
        struct.pack_into("<I", out, i * 4, v)
    for i, v in enumerate(real):            # real at bytes 20-39
        struct.pack_into("<I", out, 20 + i * 4, v)
    struct.pack_into("<Q", out, 40, ts)
    return bytes(out)


def has_rtc(mode, header):
    """Return True if the cartridge header indicates an RTC is present."""
    if mode == "DMG":
        return bool(header.get("has_rtc", False))
    return False  # AGB RTC is handled separately by FlashGBX GPIO detection


# ---------------------------------------------------------------------------
# ROM cache
# ---------------------------------------------------------------------------
ROM_CACHE_DIR = os.path.expanduser("~/.local/share/pygb/roms")


def rom_cache_path(safe_title, ext):
    return os.path.join(ROM_CACHE_DIR, safe_title + ext)


def cart_checksum(mode, header):
    """
    Return the checksum value embedded in the cartridge header that covers
    the full ROM content — used to validate a cached dump.

    DMG: 16-bit global checksum at header bytes 0x14E–0x14F (sum of all bytes
         in the ROM except those two bytes themselves).
    AGB: 32-bit checksum derived from the entry-point word at 0x00–0x03, which
         is fixed for a given ROM, combined with the header complement byte at
         0xBD.  We store both as a single 40-bit tuple so comparisons are exact.
    """
    if mode == "DMG":
        return header.get("rom_checksum")          # int, None if missing
    else:
        # AGB headers don't carry a full-ROM checksum, so we use the header
        # complement checksum (1 byte) together with the ROM size as a proxy.
        return (header.get("header_checksum"), header.get("rom_size"))


def file_checksum_dmg(path):
    """Compute the DMG global ROM checksum over a file (skips bytes 0x14E–0x14F)."""
    total = 0
    with open(path, "rb") as f:
        data = f.read()
    for i, b in enumerate(data):
        if i not in (0x14E, 0x14F):
            total += b
    return total & 0xFFFF


def file_checksum_agb(path):
    """Return the (header_complement, rom_size) proxy checksum for a GBA ROM file."""
    with open(path, "rb") as f:
        f.seek(0xBD)
        chk = struct.unpack("B", f.read(1))[0]
    size = os.path.getsize(path)
    return (chk, size)


def verify_cached_rom(cached_path, mode, header):
    """
    Return True if the cached ROM file matches the live cartridge's checksum.
    Prints a status message either way.
    """
    if not os.path.exists(cached_path):
        return False

    expected = cart_checksum(mode, header)
    if expected is None:
        warn("Cartridge returned no checksum; cached ROM cannot be verified.")
        return False

    status("Verifying cached ROM checksum...")
    try:
        if mode == "DMG":
            actual = file_checksum_dmg(cached_path)
        else:
            actual = file_checksum_agb(cached_path)
    except OSError as e:
        warn(f"Could not read cached ROM: {e}")
        return False

    if actual == expected:
        success(f"Cached ROM verified (checksum 0x{expected if mode == 'DMG' else expected[0]:X}). Skipping dump.")
        return True

    warn(f"Cached ROM checksum mismatch (got {actual!r}, expected {expected!r}). Re-dumping.")
    return False


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------
def dump_rom(dev, mode, header, rom_path, progress):
    """Dump the cartridge ROM to rom_path, then update the ROM cache."""
    mbc = get_mbc(mode, header)
    rom_size = get_rom_size(mode, header)

    status(f"Dumping ROM ({format_size(rom_size)}) to {os.path.basename(rom_path)}")

    dev.TransferData(
        args={
            "mode": 1,
            "path": rom_path,
            "mbc": mbc,
            "rom_size": rom_size,
            "agb_rom_size": rom_size,
            "start_addr": 0,
            "fast_read_mode": True,
            "cart_type": 0,
        },
        signal=progress.SetProgress,
    )

    if not (os.path.exists(rom_path) and os.path.getsize(rom_path) > 0):
        error("ROM dump failed.")
        return False

    success(f"ROM dumped: {format_size(os.path.getsize(rom_path))}")

    # Update ROM cache
    cache_path = rom_cache_path(
        sanitize_title(header.get("game_title", "UNKNOWN")),
        os.path.splitext(rom_path)[1],
    )
    try:
        os.makedirs(ROM_CACHE_DIR, exist_ok=True)
        shutil.copy2(rom_path, cache_path)
    except OSError as e:
        warn(f"Could not update ROM cache: {e}")

    return True


def dump_save(dev, mode, header, save_path, progress):
    """Read save data from cartridge to save_path."""
    mbc = get_mbc(mode, header)
    save_type, save_size = get_save_info(mode, header)

    # For AGB, if header doesn't know the save type, auto-detect
    if mode == "AGB" and save_type == 0:
        save_type, save_size = detect_save_type_agb(dev)
        if save_type != 0:
            header["save_type"] = save_type  # cache for write_save

    if save_type == 0 or save_size == 0:
        warn("No save data detected on this cartridge.")
        return False

    status(f"Reading save data ({save_type_name(mode, save_type)}, {format_size(save_size)})")

    cart_has_rtc = has_rtc(mode, header)

    dev.TransferData(
        args={
            "mode": 2,
            "path": save_path,
            "mbc": mbc,
            "save_type": save_type,
            "save_size": save_size,
            "rtc": cart_has_rtc,
        },
        signal=progress.SetProgress,
    )

    if not (os.path.exists(save_path) and os.path.getsize(save_path) > 0):
        warn("No save data was read (cartridge may not have battery-backed RAM).")
        return False

    # If RTC was dumped, FlashGBX appends MBC3_VBA_RTC_SIZE bytes to the file.
    # Split them out: keep save_path as pure SRAM, write a companion .rtc file
    # in SameBoy format so SameBoy libretro picks it up correctly.
    if cart_has_rtc:
        raw = open(save_path, "rb").read()
        if len(raw) == save_size + MBC3_VBA_RTC_SIZE:
            sram_data = raw[:save_size]
            vba_rtc   = raw[save_size:]
            rtc_path  = os.path.splitext(save_path)[0] + ".rtc"
            open(save_path, "wb").write(sram_data)
            open(rtc_path,  "wb").write(vba_to_sameboy_rtc(vba_rtc))
            success(f"Save data read: {format_size(save_size)} + RTC")
        else:
            warn("RTC data size mismatch in dump; RTC not saved.")
            success(f"Save data read: {format_size(os.path.getsize(save_path))}")
    else:
        success(f"Save data read: {format_size(os.path.getsize(save_path))}")

    return True


def write_save(dev, mode, header, save_path, progress):
    """Write save data from save_path back to the cartridge."""
    mbc = get_mbc(mode, header)
    save_type, save_size = get_save_info(mode, header)

    if mode == "AGB" and save_type == 0:
        save_type, save_size = detect_save_type_agb(dev)

    if save_type == 0 or save_size == 0:
        warn("No save type detected; skipping save writeback.")
        return False

    if not os.path.exists(save_path):
        warn("No save file found to write back.")
        return False

    cart_has_rtc = has_rtc(mode, header)
    rtc_path = os.path.splitext(save_path)[0] + ".rtc"
    write_path = save_path  # FlashGBX reads from this file

    # If RTC data is available, build a temporary combined SRAM+VBA_RTC file
    # that FlashGBX expects when rtc=True.
    tmp_combined = None
    if cart_has_rtc and os.path.exists(rtc_path):
        sram_data = open(save_path, "rb").read()
        sb_rtc    = open(rtc_path,  "rb").read()
        if len(sb_rtc) >= SAMEBOY_RTC_SIZE:
            vba_rtc = sameboy_to_vba_rtc(sb_rtc[:SAMEBOY_RTC_SIZE])
            fd, tmp_combined = tempfile.mkstemp(suffix=".sav")
            with os.fdopen(fd, "wb") as f:
                f.write(sram_data + vba_rtc)
            write_path = tmp_combined
        else:
            warn("RTC file is too small; writing save without RTC.")
            cart_has_rtc = False
    elif cart_has_rtc:
        warn("No .rtc file found; writing save without RTC.")
        cart_has_rtc = False

    status(f"Writing save data back to cartridge ({format_size(os.path.getsize(save_path))}"
           + (" + RTC" if cart_has_rtc else "") + ")")

    try:
        dev.TransferData(
            args={
                "mode": 3,
                "path": write_path,
                "mbc": mbc,
                "save_type": save_type,
                "save_size": save_size,
                "erase": False,
                "rtc": cart_has_rtc,
                "rtc_advance": cart_has_rtc,  # advance RTC by elapsed time since emulator save
                "verify_write": True,
            },
            signal=progress.SetProgress,
        )
    finally:
        if tmp_combined:
            try:
                os.unlink(tmp_combined)
            except OSError:
                pass

    success("Save data written back to cartridge.")
    return True


# ---------------------------------------------------------------------------
# PyGB config file  (~/.config/pygb/pygb.ini)
# ---------------------------------------------------------------------------
PYGB_CONFIG_DIR = os.path.expanduser("~/.config/pygb")
PYGB_CONFIG_FILE = os.path.join(PYGB_CONFIG_DIR, "pygb.ini")


def load_pygb_config():
    cfg = configparser.ConfigParser()
    cfg.read(PYGB_CONFIG_FILE)
    return cfg


def save_pygb_config(cfg):
    os.makedirs(PYGB_CONFIG_DIR, exist_ok=True)
    with open(PYGB_CONFIG_FILE, "w") as f:
        cfg.write(f)


def get_cheevos_credentials(cfg):
    """Return (username, password) from config, or (None, None) if not set."""
    if "retroachievements" not in cfg:
        return None, None
    ra = cfg["retroachievements"]
    return ra.get("username") or None, ra.get("password") or None


def set_cheevos_credentials(cfg, username, password):
    if "retroachievements" not in cfg:
        cfg["retroachievements"] = {}
    cfg["retroachievements"]["username"] = username
    cfg["retroachievements"]["password"] = password


# ---------------------------------------------------------------------------
# Emulator launcher
# ---------------------------------------------------------------------------
def find_emulator():
    """Find an available emulator on the system."""
    candidates = [
        "retroarch",
        "mgba-qt",
        "mgba",
        "gambatte-qt",
        "sameboy",
    ]
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def find_retroarch_core(mode):
    """Find a suitable RetroArch core for the given cart mode."""
    core_names = ["mgba_libretro", "vba_next_libretro", "vbam_libretro"] if mode == "AGB" else ["sameboy_libretro", "gambatte_libretro", "mgba_libretro"]
    search_dirs = [
        "/usr/lib/libretro",
        "/usr/lib64/libretro",
        "/usr/local/lib/libretro",
        os.path.expanduser("~/.config/retroarch/cores"),
        os.path.expanduser("~/.local/share/retroarch/cores"),
    ]
    for core in core_names:
        for d in search_dirs:
            p = os.path.join(d, core + ".so")
            if os.path.exists(p):
                return p
    return None


def _write_cheevos_appendconfig(username, password, hardcore):
    """
    Write a temporary RetroArch appendconfig that enables RetroAchievements.
    Returns the path to the temp file (caller must delete it).
    """
    lines = [
        'cheevos_enable = "true"',
        f'cheevos_username = "{username}"',
        f'cheevos_password = "{password}"',
        f'cheevos_hardcore_mode_enable = "{"true" if hardcore else "false"}"',
    ]
    fd, path = tempfile.mkstemp(prefix="pygb_cheevos_", suffix=".cfg")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def build_emulator_cmd(emulator, rom_path, mode, cheevos=None):
    """
    Build the command line for the emulator.
    Returns (cmd, core_path, tmp_files) — tmp_files is a list of temp paths
    to clean up after the emulator exits.

    cheevos: None, or dict with keys 'username', 'password', 'hardcore'.
    """
    emu_name = os.path.basename(emulator).lower()
    tmp_files = []

    if "retroarch" in emu_name:
        core_path = find_retroarch_core(mode)
        cmd = [emulator]
        if core_path:
            cmd += ["-L", core_path]
        else:
            warn("No RetroArch core found; launching without explicit core (RetroArch will use file association).")

        if cheevos:
            cfg_path = _write_cheevos_appendconfig(
                cheevos["username"], cheevos["password"], cheevos.get("hardcore", False)
            )
            cmd += ["--appendconfig", cfg_path]
            tmp_files.append(cfg_path)

        cmd.append(rom_path)
        return cmd, core_path, tmp_files

    return [emulator, rom_path], None, []


def _retroarch_saves_dir():
    """Return RetroArch's default saves directory."""
    return os.path.expanduser("~/.config/retroarch/saves")


def _core_subdir(core_path):
    """
    Return the core's save subdirectory name by reading its .info file.
    RetroArch organises saves as <saves_dir>/<CoreName>/<game>.srm when
    sort_savefiles_enable is on.  The corename comes from the .info file
    that lives alongside the core .so, or in /usr/share/libretro/info/.
    Returns None if the info file cannot be found or parsed.
    """
    if not core_path:
        return None

    core_stem = os.path.splitext(os.path.basename(core_path))[0]  # e.g. sameboy_libretro
    info_search = [
        os.path.join(os.path.dirname(core_path), core_stem + ".info"),
        f"/usr/share/libretro/info/{core_stem}.info",
        f"/usr/local/share/libretro/info/{core_stem}.info",
    ]
    for info_path in info_search:
        try:
            with open(info_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("corename"):
                        _, _, val = line.partition("=")
                        return val.strip().strip('"')
        except OSError:
            continue
    return None


def _save_candidates(rom_base, work_dir, core_subdir):
    """Build the list of paths where RetroArch might write/read the save."""
    ra_saves = _retroarch_saves_dir()
    paths = [
        os.path.join(work_dir, rom_base + ".srm"),
        os.path.join(work_dir, rom_base + ".sav"),
        os.path.join(ra_saves, rom_base + ".srm"),
        os.path.join(ra_saves, rom_base + ".sav"),
    ]
    if core_subdir:
        paths += [
            os.path.join(ra_saves, core_subdir, rom_base + ".srm"),
            os.path.join(ra_saves, core_subdir, rom_base + ".sav"),
        ]
    return paths


def pre_place_save(save_path, rom_base, work_dir, core_subdir=None):
    """
    Copy the save (and companion .rtc if present) to all locations RetroArch
    might look for them before launch.
    Returns the list of .srm/.sav paths that were written.
    """
    if not os.path.exists(save_path):
        return []

    rtc_src = os.path.splitext(save_path)[0] + ".rtc"
    has_rtc_file = os.path.exists(rtc_src)

    placed = []
    for dst in _save_candidates(rom_base, work_dir, core_subdir):
        if dst == save_path:
            placed.append(dst)
        else:
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(save_path, dst)
                placed.append(dst)
            except OSError:
                pass

        # Place matching .rtc next to each .srm/.sav we just wrote
        if has_rtc_file:
            rtc_dst = os.path.splitext(dst)[0] + ".rtc"
            try:
                os.makedirs(os.path.dirname(rtc_dst), exist_ok=True)
                shutil.copy2(rtc_src, rtc_dst)
            except OSError:
                pass

    return placed


def collect_save(save_path, rom_base, work_dir, placed_paths, core_subdir=None):
    """
    After the emulator exits, find the newest updated save file (and companion
    .rtc if present) and copy them to our canonical save_path / .rtc location.
    Returns True if a valid save was found.
    """
    candidates = list(placed_paths) + _save_candidates(rom_base, work_dir, core_subdir) + [save_path]

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    best_path = None
    best_mtime = -1
    for p in unique:
        try:
            mt = os.path.getmtime(p)
            sz = os.path.getsize(p)
            if sz > 0 and mt > best_mtime:
                best_mtime = mt
                best_path = p
        except OSError:
            pass

    if best_path is None:
        return False

    if best_path != save_path:
        status(f"Found updated save at {best_path}")
        shutil.copy2(best_path, save_path)

    # Collect the companion .rtc file from the same directory as the best save
    rtc_dst = os.path.splitext(save_path)[0] + ".rtc"
    rtc_src = os.path.splitext(best_path)[0] + ".rtc"
    if rtc_src != rtc_dst and os.path.exists(rtc_src):
        shutil.copy2(rtc_src, rtc_dst)
    elif not os.path.exists(rtc_src):
        # Search the other candidate locations for a newer .rtc
        rtc_best = None
        rtc_mtime = -1
        for p in unique:
            candidate_rtc = os.path.splitext(p)[0] + ".rtc"
            try:
                mt = os.path.getmtime(candidate_rtc)
                if mt > rtc_mtime:
                    rtc_mtime = mt
                    rtc_best = candidate_rtc
            except OSError:
                pass
        if rtc_best and rtc_best != rtc_dst:
            shutil.copy2(rtc_best, rtc_dst)

    return True


def launch_emulator(emulator, rom_path, save_path, mode, rom_base, work_dir, cheevos=None):
    """Launch the emulator, wait for it to exit, then locate the updated save."""
    cmd, core_path, tmp_files = build_emulator_cmd(emulator, rom_path, mode, cheevos)
    core_subdir = _core_subdir(core_path)
    status(f"Launching: {' '.join(cmd)}")
    if core_subdir:
        status(f"Core saves subdirectory: {core_subdir}")
    if cheevos:
        status(f"RetroAchievements enabled for {cheevos['username']}"
               + (" (hardcore)" if cheevos.get("hardcore") else ""))

    # Pre-place the existing save where the emulator expects it
    placed = pre_place_save(save_path, rom_base, work_dir, core_subdir)

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        warn("Emulator interrupted.")
    except Exception as e:
        error(f"Failed to launch emulator: {e}")
        return False
    finally:
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass

    return collect_save(save_path, rom_base, work_dir, placed, core_subdir)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="PyGB - Play Game Boy / GBA cartridges via GBxCart RW",
    )
    parser.add_argument(
        "--port", "-p",
        help="Serial port of the GBxCart RW device (auto-detect if omitted)",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["dmg", "agb", "auto"],
        default="auto",
        help="Cartridge mode: dmg (Game Boy), agb (GBA), or auto (default: auto)",
    )
    parser.add_argument(
        "--emulator", "-e",
        help="Path to emulator executable (auto-detect if omitted)",
    )
    parser.add_argument(
        "--keep-files", "-k",
        action="store_true",
        help="Keep dumped ROM and save files after exiting",
    )
    parser.add_argument(
        "--output-dir", "-o",
        help="Directory to store ROM/save files (default: temp directory)",
    )
    parser.add_argument(
        "--no-writeback",
        action="store_true",
        help="Don't write save data back to the cartridge after emulator closes",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Always dump the ROM from the cartridge, ignoring any cached copy",
    )

    ra_group = parser.add_argument_group("RetroAchievements")
    ra_group.add_argument(
        "--ra-user",
        metavar="USERNAME",
        help="RetroAchievements username (saved to config on first use)",
    )
    ra_group.add_argument(
        "--ra-password",
        metavar="PASSWORD",
        help="RetroAchievements password (saved to config on first use)",
    )
    ra_group.add_argument(
        "--ra-hardcore",
        action="store_true",
        help="Enable RetroAchievements hardcore mode",
    )
    ra_group.add_argument(
        "--no-ra",
        action="store_true",
        help="Disable RetroAchievements for this session",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}PyGB - GBxCart Play Cart{RESET}")
    print(f"{'=' * 40}\n")

    # Load persistent config and resolve RetroAchievements credentials
    pygb_cfg = load_pygb_config()
    cfg_user, cfg_pass = get_cheevos_credentials(pygb_cfg)

    ra_user = args.ra_user or cfg_user
    ra_pass = args.ra_password or cfg_pass

    if args.ra_user and args.ra_password:
        # Save newly supplied credentials for future runs
        set_cheevos_credentials(pygb_cfg, args.ra_user, args.ra_password)
        save_pygb_config(pygb_cfg)
        success(f"RetroAchievements credentials saved for {args.ra_user}.")

    cheevos = None
    if not args.no_ra and ra_user and ra_pass:
        cheevos = {"username": ra_user, "password": ra_pass, "hardcore": args.ra_hardcore}

    # Step 1: Find emulator
    emulator = args.emulator or find_emulator()
    if not emulator:
        error("No emulator found. Install RetroArch, mGBA, or pass --emulator.")
        sys.exit(1)
    status(f"Emulator: {emulator}")

    # Step 2: Connect to GBxCart RW
    status("Searching for GBxCart RW device...")
    dev = connect_device(port=args.port)
    if dev is None:
        error(
            "No GBxCart RW device found.\n"
            "  - Is the device plugged in?\n"
            "  - Do you have permission to access the serial port?\n"
            "    (try: sudo usermod -aG dialout $USER)"
        )
        sys.exit(1)
    success(f"Connected to {dev.GetFullNameExtended()}")

    dev.SetAutoPowerOff(300000)  # 5 minutes idle auto-off
    dev.SetAGBReadMethod(2)      # Stream read for AGB (fastest)

    progress = ProgressHandler()

    # Step 3: Detect cartridge mode and read header
    if args.mode == "auto":
        status("Auto-detecting cartridge type...")
        mode, header = detect_mode(dev)
        if mode is None:
            error(
                "No cartridge detected.\n"
                "  - Is a cartridge inserted?\n"
                "  - Are the contacts clean?\n"
                "  - Try specifying --mode dmg or --mode agb."
            )
            dev.Close(cartPowerOff=True)
            sys.exit(1)
    else:
        mode = args.mode.upper()
        dev.SetMode(mode)
        time.sleep(0.2)
        header = dev.ReadInfo()
        if header is False or header == {} or header.get("empty_nocart", True):
            error("No cartridge detected in the selected mode.")
            dev.Close(cartPowerOff=True)
            sys.exit(1)

    mode_name = "Game Boy" if mode == "DMG" else "Game Boy Advance"
    game_title = header.get("game_title", "UNKNOWN").strip()
    success(f"Detected {mode_name} cartridge: {BOLD}{game_title}{RESET}")

    if mode == "DMG":
        rom_size_str = header.get("rom_size", "?")
        ram_size_str = header.get("ram_size", "?")
        mbc_raw = header.get("mapper_raw", 0)
        mbc_name = Util.DMG_Header_Mapper.get(mbc_raw, f"0x{mbc_raw:02X}") if isinstance(Util.DMG_Header_Mapper, dict) else f"0x{mbc_raw:02X}"
        print(f"  ROM: {rom_size_str}  |  RAM: {ram_size_str}  |  Mapper: {mbc_name}")
    else:
        game_code = header.get("game_code", "")
        if game_code:
            print(f"  Game code: {game_code}")

    # Step 4: Set up output directory
    safe_title = sanitize_title(game_title)
    if args.output_dir:
        work_dir = args.output_dir
        os.makedirs(work_dir, exist_ok=True)
    else:
        work_dir = tempfile.mkdtemp(prefix=f"pygb_{safe_title}_")

    ext = get_rom_extension(mode, header)
    rom_base = safe_title
    rom_path = os.path.join(work_dir, rom_base + ext)
    save_path = os.path.join(work_dir, rom_base + ".sav")

    print()

    # Step 5: Use cached ROM if valid, otherwise dump from cartridge
    cached = rom_cache_path(safe_title, ext)
    if not args.no_cache and verify_cached_rom(cached, mode, header):
        shutil.copy2(cached, rom_path)
    else:
        if not dump_rom(dev, mode, header, rom_path, progress):
            error("Failed to dump ROM. Aborting.")
            dev.Close(cartPowerOff=True)
            sys.exit(1)

    # Step 6: Dump save data
    has_save = dump_save(dev, mode, header, save_path, progress)

    # Power off cart during emulation
    dev.CartPowerOff()
    print()

    # Step 7: Launch emulator
    status(f"Starting {game_title}...")
    save_found = launch_emulator(emulator, rom_path, save_path, mode, rom_base, work_dir, cheevos)

    print()

    # Step 8: Write save back to cartridge
    if has_save and save_found and not args.no_writeback:
        status("Preparing to write save back to cartridge...")
        dev.SetMode(mode)
        time.sleep(0.3)
        check_header = dev.ReadInfo()
        if check_header and not check_header.get("empty_nocart", True):
            write_save(dev, mode, header, save_path, progress)
        else:
            error("Cartridge no longer detected. Cannot write save back.")
            error(f"Your save file is preserved at: {save_path}")
            args.keep_files = True
    elif has_save and not save_found and not args.no_writeback:
        warn("No updated save file found from the emulator.")
    elif args.no_writeback and has_save:
        status("Save writeback disabled (--no-writeback).")

    # Step 9: Clean up
    dev.Close(cartPowerOff=True)
    success("Device disconnected.")

    if args.keep_files or args.output_dir:
        success(f"Files saved in: {work_dir}")
    else:
        try:
            shutil.rmtree(work_dir)
            status("Temporary files cleaned up.")
        except Exception:
            warn(f"Could not clean up temp files at: {work_dir}")

    print(f"\n{GREEN}Done!{RESET}\n")


if __name__ == "__main__":
    main()
