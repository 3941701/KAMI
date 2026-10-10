# KAMI
Kot And Mouse Injector

Inject mouse movements into emulated games

# Requirements
Common requirements:
* Windows
* [.NET 10 Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/10.0)

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
* `when` on a camera makes it apply only while a value in memory satisfies a condition (`==`, `!=`, `&`, `<`, `>`); the first camera that applies is used. Use it for games with several camera modes.
* `writes` are values forced on every tick while injecting, for example to switch off auto-centering.
* `"trace": true` makes KAMI append what it reads and writes to `kami_trace.csv`.
* `"hold": true` on a `HAVA` camera is for games whose camera swings behind the player or levels itself out on its own while the character moves. Normally KAMI reads the angle, adds the mouse movement and writes the sum, so the game's own movement stays in the angle and piles up with the mouse. With `hold` KAMI keeps its own angle and writes it every tick (the mouse sets the angle outright, the game's nudges do not accumulate), but the game's own camera input (gamepad stick) no longer moves the camera while it is on. A jump of more than `hold_resync` radians (default `0.5`) in one tick is taken as the game's own decision (cut-scene, respawn, camera reset) and the angle is picked up from the game again. Off by default.

See `KAMI.Core/profiles/_example.json` for all fields. Once a profile is stable it can be turned into a regular game class.

# Building
## Requirements
* .Net 10 SDK
* To build dependencies, needs C++ build tools and meson + ninja

## How to build
* To build dependencies, run `build_deps.bat` in 'x64 Native Tools Command Prompt for VS 2026'
* To build KAMI using commandline, run `build_publish.bat` in 'x64 Native Tools Command Prompt for VS 2026'
* To build KAMI using Visual Studio, open `KAMI.sln` and press the big green button.
