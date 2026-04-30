# PyGB

Play physical Game Boy, Game Boy Color, and Game Boy Advance cartridges through an emulator using a [GBxCart RW](https://www.gbxcart.com/) USB cartridge reader/writer.

PyGB dumps the ROM and save from a real cartridge, launches an emulator, and writes the updated save back to the cartridge when you close it — so your progress on the real hardware and the emulator stays in sync.

## What it does

1. Detects the connected cartridge and reads its header
2. Dumps the ROM (uses a checksum-verified cache on subsequent runs to save time)
3. Reads the save data (including RTC for MBC3 carts like Pokémon Gold/Silver)
4. Launches an emulator with the dumped ROM and save pre-loaded
5. When you close the emulator, writes the updated save back to the cartridge
6. Shows a GUI status window throughout the process (works when launched from a file manager)

## Requirements

**Hardware**
- [GBxCart RW v1.4](https://www.gbxcart.com/) (firmware R30 or newer)

**Software**
- Python 3.9+
- [FlashGBX](https://github.com/lesserkuma/FlashGBX) (`pip install FlashGBX`)
- An emulator — RetroArch is recommended, mGBA and SameBoy also work

**RetroArch cores** (auto-detected):
| System | Preferred core | Fallbacks |
|--------|----------------|-----------|
| GB / GBC | SameBoy | Gambatte, mGBA |
| GBA | mGBA | VBA Next, VBA-M |

**Linux serial port access**

Add yourself to the `dialout` group so Python can open `/dev/ttyUSB0` without `sudo`:

```sh
sudo usermod -aG dialout $USER
# Log out and back in for the change to take effect
```

## Installation

```sh
git clone https://github.com/Kannamoris/pygb
cd pygb
pip install FlashGBX
chmod +x pygb.py
```

### Running from a file manager (Dolphin, Nautilus, etc.)

Mark the script executable and double-click it. PyGB opens a GUI window showing connection status, transfer progress, and a Close button when done — no terminal needed.

To associate `.py` files with a terminal in Dolphin: right-click → Properties → Open With → add your terminal emulator.

## Usage

```
./pygb.py [options]
```

Just plug in the GBxCart RW with a cartridge inserted and run the script — everything else is automatic.

### Options

| Flag | Description |
|------|-------------|
| `-p`, `--port PORT` | Serial port of the GBxCart RW (auto-detected if omitted) |
| `-m`, `--mode {dmg,agb,auto}` | Force cartridge mode instead of auto-detecting |
| `-e`, `--emulator PATH` | Path to emulator executable (auto-detected if omitted) |
| `-k`, `--keep-files` | Keep the dumped ROM and save files after exiting |
| `-o`, `--output-dir DIR` | Write ROM/save files here instead of a temp directory |
| `--no-writeback` | Skip writing the save back to the cartridge on exit |
| `--no-cache` | Always re-dump the ROM even if a cached copy exists |
| `--no-gui` | Suppress the GUI window (terminal output only) |

### RetroAchievements

```
./pygb.py --ra-user YOUR_USERNAME --ra-password YOUR_PASSWORD
```

Credentials are saved to `~/.config/pygb/pygb.ini` on first use — you only need to pass them once.

| Flag | Description |
|------|-------------|
| `--ra-user USERNAME` | RetroAchievements username |
| `--ra-password PASSWORD` | RetroAchievements password |
| `--ra-hardcore` | Enable hardcore mode (no save states or rewind) |
| `--no-ra` | Disable RetroAchievements for this session |

## ROM cache

Dumping a ROM takes 30–90 seconds depending on size. PyGB caches every dump in `~/.local/share/pygb/roms/` and verifies the cache against the cartridge's built-in checksum on subsequent runs. If the checksum matches, the dump is skipped entirely.

Pass `--no-cache` to force a fresh dump regardless.

## RTC support

MBC3 cartridges with a real-time clock (Pokémon Gold, Silver, Crystal) are fully supported. PyGB:

- Dumps the RTC registers alongside the save
- Converts between the FlashGBX/VBA 48-byte format and SameBoy's 32-byte libretro format
- Writes the RTC back to the cartridge with the elapsed time since the emulator save automatically applied

## File locations

| Path | Contents |
|------|----------|
| `~/.local/share/pygb/roms/` | ROM cache |
| `~/.config/pygb/pygb.ini` | Saved credentials and settings |

On Windows: `%LOCALAPPDATA%\pygb\` and `%APPDATA%\pygb\` respectively.  
On macOS: `~/Library/Application Support/pygb/` for both.

## Platform support

| Platform | Status |
|----------|--------|
| Linux | Tested |
| Windows | Should work — serial port auto-detection uses `COM*` ports |
| macOS | Should work — untested |
