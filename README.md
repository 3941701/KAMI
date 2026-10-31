# KAMI
Kot And Mouse Injector

Inject mouse movements into emulated games

# About this fork
This fork adds games described by JSON profiles instead of compiled classes, so a new game can be added and tuned without rebuilding KAMI:

* `ProfileGame`: profiles in the `profiles` folder, re-read while KAMI runs (see [Adding a game with a profile](#adding-a-game-with-a-profile)).
* `"hold": true` for cameras the game keeps turning on its own (auto-follow, auto-level).
* For games with an aim or zoom camera: `scoped` (with the *Scoped Sensitivity* field), `mirror`, `hold_grace` and `max_speed` on cameras; conditions over several values and over how fast a value changes; writes that copy a value.
* PS3 games running natively in a PC port, starting with the inFAMOUS port (see [Native PC ports](#native-pc-ports)).
* A ready profile for inFAMOUS (see [inFAMOUS](#infamous)).
* All projects target .NET 8 (`net8.0`): builds for .NET 9 and .NET 10 did not start on the machine this fork is tested on, even with those runtimes installed.

The Windows build is made by GitHub Actions on every push and pull request (artifact `KAMI-Windows` on the run page). Commits that only change `*.md` files do not start a build.

# Requirements
Common requirements:
* Windows
* [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/8.0) (x64) for the builds of this fork; upstream KAMI needs the [.NET 10 Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/10.0)

For RPCS3:
* A recent build with IPC enabled, oldest supported build is `0.0.22-13800`

For PCSX2:
* A recent devbuild with IPC enabled, oldest supported build is `v1.7.0-dev-1177-gedeb0d7bd`
* Use IPC Port 28012

# Compatibility
The compatibility list can be found [here](https://isjuhn.github.io/index.html).

# Running
The application will automatically detect if RPCS3 is running and which game is running. If the game is supported it will automatically connect. If it doesn't, make sure IPC is enabled, you can find it under Configuration -> IPC.

For RPCS3 the only thing you need to change is the On/Off bind, the button you bind to this will act as a global hotkey and right now KAMI will eat those inputs. Therefore it's wise to pick a button you will have no use for when the application is running. Additionally you can change the sensitivity to fit your needs, the units for sensitivity right now is number of radians of change per pixel moved. The green/red circle next to the sensitvity indicates if the sensitivity is applied.

For PCSX2 it can also be beneficial to use the Mouse1 and Mouse2 binds since you cannot combine PCSX2 mouse bindings with KAMI. There is also the option of hiding your mouse cursor when injecting, this is also useful for PCSX2 or if you're running RPCS3 windowed.

Note:
* binding Mouse1 and/or Mouse2 uses [low-level mouse hooks](https://docs.microsoft.com/en-us/previous-versions/windows/desktop/legacy/ms644986(v=vs.85))
* Hiding the mouse cursor sets your system cursor to an invisible cursor and to show your cursor again, KAMI will reload your system cursors

These features are rather intrusive and as such they are entirely optional. For RPCS3, none of them are needed and I hope once PCSX2 moves to QT the need for these features will be gone.

## Native PC ports
With the emulator set to RPCS3, KAMI also connects to a PS3 game running natively in a static-recompilation PC port, instead of RPCS3, while one is running. It reads and writes the port's copy of the PS3 memory directly, so a profile made on RPCS3 applies unchanged, and it updates the camera every millisecond rather than every 8 ms. Supported: the inFAMOUS port (whompay/inFAMOUSreborn and its forks), process `inFamousRecomp.exe`, reported as `BCES00220` 01.00 with the RPCS3 hash of the same executable. Turn off the port's own mouse look while KAMI drives the camera, and set `"MouseHandler": "RawInput"` in `config.json`: the port keeps moving the cursor back to the middle of its window, which fights KAMI's default cursor mode. If KAMI does not connect, `native_port.log` next to `KAMI.exe` says why.

# Adding a game with a profile
A game can be described by a JSON file instead of a compiled class. Put the file in the `profiles` folder next to `KAMI.exe`; it is picked up while KAMI is running, takes precedence over a built-in game with the same ID, and is re-read whenever it changes, so addresses, signs and limits can be tuned with the game running. Files whose name starts with `_` are ignored.

A profile lists one or more builds of a game. A build is matched by the executable hash that KAMI shows as `Hash` (preferred, it is unambiguous) or by title ID and version. Each build has one or more cameras, in one of the three models the built-in games use:

| `type` | what the game stores | built-in equivalent |
| --- | --- | --- |
| `HAVA` | horizontal and vertical angle as two scalars | `HAVACamera` |
| `HVecVA` | horizontal sin/cos pair plus a vertical scalar | `HVecVACamera` |
| `HVVec` | one 3D direction vector | `HVVecCamera` |

Addresses are pointer chains with the semantics of `DerefChain`: `"base": "0x00A12340", "offsets": ["0x5C", "0x118", "0x1A8"]` means read the pointer at `0x00A12340`, add `0x5C`, read the pointer there, add `0x118`, read the pointer there, add `0x1A8`. An empty `offsets` list is a plain static address.

* `sign` is the direction the stored value moves for a positive mouse delta (mouse right for `hor`, mouse down for `vert`); flip it if the camera turns the wrong way.
* `unit` is `rad`, `deg` or `u16` (65536 per turn). `min`, `max` and `wrap` are given in that unit.
* `scale` multiplies the mouse delta for one axis (default `1`).
* `when` on a camera makes it apply only while a value in memory satisfies a condition (`==`, `!=`, `&`, `<`, `>`); the first camera that applies is used. Use it for games with several camera modes. A list of conditions applies only while all of them hold. With `"rate": true` a condition compares how fast the value changes, per second, with `<` or `>` (for example a camera distance that drops because the camera hit a wall), and `"for"` (seconds) keeps a condition true for that long after it last held.
* `writes` are values (`float`, `u8`, `u16`, `u32`) forced on every tick while injecting, for example to switch off auto-centering or to keep a state machine of the game from starting. They are written only while injection is on: with KAMI switched off by its hotkey the game behaves as without KAMI. A write can have a `when` of its own, the same as a camera. Instead of a fixed `value` a write can take `copy`, a chain whose current value (of the write's `type`) is written, for example a game clock copied into a time stamp.
* `"trace": true` makes KAMI write what it reads and writes to `kami_trace_<date>_<time>.csv`, a new file per session: a line whenever the mouse or the game moved the camera or a traced value changed, and at least every 250 ms, with the angle the game had before the tick (`game_hor`, `game_vert`) next to the one KAMI holds after it. `trace_fields` adds columns: a list of chains like a write's, each with a `name` and a `type`.
* `"hold": true` on a `HAVA` camera is for games whose camera swings behind the player or levels itself out on its own while the character moves. Normally KAMI reads the angle, adds the mouse movement and writes the sum, so the game's own movement stays in the angle and piles up with the mouse. With `hold` KAMI keeps its own angle and writes it every tick (the mouse sets the angle outright, the game's nudges do not accumulate), but the game's own camera input (gamepad stick) no longer moves the camera while it is on. A jump of more than `hold_resync` radians (default `0.5`) in one tick is taken as the game's own decision (cut-scene, respawn, camera reset) and the angle is picked up from the game again. A camera that is not used on a tick (its `when` is false, or an earlier camera applies) forgets what it held, so to let the game take the camera back in some state, put a camera with `"hold": false` and a `when` for that state before the one with `hold`. Off by default.
* `hold_grace` (seconds, default `0`) on a camera with `hold`: for this long after the camera is picked up again (injection started, its `when` became true, or another camera stopped applying) it follows the game instead of holding, so a snap the game makes when switching modes, such as turning the camera to where the player aimed, is kept rather than undone.
* `"scoped": true` on a camera scales the mouse movement by KAMI's *Scoped Sensitivity* while that camera is used, for an aim or zoom camera. The *Scoped Sensitivity* field in the window is editable while such a profile is loaded.
* `max_speed` (radians per second, default `0`, no limit) on a camera caps how fast the mouse turns it; the rest of a faster movement is dropped. Use it on a camera with a `when` for a state in which the game cannot cope with fast turns, such as the camera being pressed against a wall.
* `mirror` on a `HAVA` camera is a list of other angle pairs kept equal to the camera's own while it is used, each with an optional `hor` and `vert` given like a camera axis (chain, `unit`, `min`, `max`, `wrap`) plus `factor` (default: the entry's `sign`, else `1`) and `offset` (in that unit, default `0`): the value written is `factor × angle + offset`. Use it when a game has a second camera (for example for aiming) that it starts from or snaps back to, so that one never lags behind the mouse. Nothing is mirrored while a `hold` camera is in its `hold_grace` period.

See `KAMI.Core/profiles/_example.json` for all fields. Numbers can be written as JSON numbers or as strings (`"0x1A8"`), and the files may contain `//` comments and trailing commas.

Which file is used:
* Keep one active profile per game in `profiles`. To keep an alternative next to it, start its name with `_`. When several files match the running game, the first by name wins.
* The active file is re-read whenever it is saved. A different file (a new one, or one renamed from `_...`) is picked up the next time KAMI detects the game, for example after restarting the game.
* A profile that cannot be parsed is reported once per save, and the last working version stays in use.

Once a profile is stable it can be turned into a regular game class, but a class would have to reimplement `hold`, `when` and `writes` by hand, so a profile that uses them is best kept as a profile.

# inFAMOUS
`KAMI.Core/profiles/BCES00609.json` is copied next to `KAMI.exe` by the build. It is matched by the executable hash `PPU-d0c3e2a107c47d20d3316325ce6f75464a8f4f1d` (EU disc BCES00609 / BCES00591, version 01.00) and was tested on RPCS3 0.0.43:

* The camera is one object on the heap. A pointer to it is at `0x00913964` in the game's data segment; yaw is the float at `+0x180` (radians, -pi..pi, decreases when turning right) and pitch the float at `+0x184` (radians, about -1.414..1.414, decreases when looking up).
* `"hold": true`, because the game turns the camera behind the character while walking and that piled up with the mouse.
* Two `writes` set `+0x29C` and `+0x1A8` to 0 every tick. When the camera touches a wall and the camera stick is not held, the game sets the word at `+0x29C` to 1 and, about 0.12 s later, the camera state at `+0x1A8` to 2, which takes the camera over and swings it behind the character (felt as the camera bouncing off the wall). Clearing both words keeps that from starting.

Not checked yet: aiming, climbing, vehicles and cut-scenes may use another camera object or another state, which would need a camera with `when`. To go back to plain mouse control, remove `hold` and the two `writes` from the file; it is re-read while KAMI runs.

Other builds of inFAMOUS (US disc, PSN) have other executables and need their own addresses.

# Building
## Requirements
* .NET 8 SDK or newer (the projects target `net8.0`)
* To build dependencies, needs C++ build tools and meson + ninja

## How to build
* To build dependencies, run `build_deps.bat` in 'x64 Native Tools Command Prompt for VS 2026'
* To build KAMI using commandline, run `build_publish.bat` in 'x64 Native Tools Command Prompt for VS 2026'
* To build KAMI using Visual Studio, open `KAMI.sln` and press the big green button.
