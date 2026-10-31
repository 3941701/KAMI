Мышь для **inFAMOUS** (PS3, европейская версия 1.00) на RPCS3 через KAMI: сырая мышь в свободной камере и в прицеле, отдельная чувствительность прицела и «Точности», игра сама не доворачивает камеру, а «Грозовая буря» водится мышью вместо наклона геймпада. Игра пройдена от начала до конца с этим набором.

Скачайте архив ниже и начните с `README.md` внутри. В архиве:

* `KAMI/`: сборка KAMI с поддержкой профилей, профиль inFAMOUS уже лежит в `KAMI/profiles`;
* `README.md`: установка, раскладка, что настраивать, что делать, если не работает;
* `patches/BCES00609_patch.yml`: патч RPCS3, в нём нужно включить три пункта;
* `profiles/`: профиль с бурей мышью и профиль без неё;
* `kami/config.example.json`: настройки KAMI автора.

Нужно: Windows 64-bit, [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/8.0) (x64), RPCS3 с включённым IPC и игра с хешем `PPU-d0c3e2a107c47d20d3316325ce6f75464a8f4f1d` (BCES00609, BCES00220, BCES00591). Американская версия не поддерживается.

Известная проблема: у некоторых стен камера проседает внутрь или резко разворачивается.

---

Mouse-look for **inFAMOUS** (PS3, EU v1.00) on RPCS3 through KAMI: raw mouse in the free and the aim camera, separate sensitivity for aiming and Precision, no automatic camera pulls, and the Lightning Storm steered with the mouse instead of the SIXAXIS tilt. The archive holds the KAMI build with the profile in place, the RPCS3 patch (enable its three entries), both profiles and a KAMI config example; start with `README.md`. Needs the .NET 8 Desktop Runtime (x64) and the executable hash above. Known issue: the camera near some walls.
