# videobot - WizardScan standalone video-generation bot - Deploy Guide

## Kya hai yeh

`videobot.py` ek **standalone** video-generation bot hai. Sirf ONE kaam
karta hai: WizardScan ke saare ffmpeg video renders karna, taaki main
bot ko video ka load na uthana pade.

**Background:** main bot 95-156% CPU aur 6.8 GB RAM par chal raha hai.
Uska video kaam load ka bara hissa hai:
- champion X-alert base pre-render, har 60 second (ffmpeg, ~8-10s each)
- rozana Most Shared video render (25 Nov 2026 ko TimeoutError se fail
  hui jab main bot 156% CPU par tha, is liye post text-only gayi)
- har champion alert ki final video (warm overlay compose, ~12-16s)
- rank-up videos

**Yeh main bot ko REPLACE nahi karta.** Main bot posting, leaderboard,
dip alerts, champions, Most Shared post, Binance, X alerts (monbot ke
saath), dropped calls (dropbot ke saath) - sab kuch pehle ki tarah karta
hai. Sirf ffmpeg **rendering** ka load main bot se hata kar videobot ko
de diya gaya hai.

## Design (safe, monbot/dropbot pattern, no shared writes)

- **Videobot** (is folder me): main bot ki likhi hui chhoti JSON request
  files ko `/opt/videobot/queue/` se padhta hai, usi renderer modules se
  render karta hai jo main bot aaj use karta hai (live `/opt/bot` se
  copy kiye gaye), aur finished `.mp4` + `.done` status file ko
  `/opt/videobot/out/` me likhta hai. Phir request file delete kar deta
  hai.
- **Main bot**: ab kabhi ffmpeg nahi chalata. Champion prerender job
  (60s, 80% threshold - dono UNCHANGED) sirf ek `champ_base` request
  likhta hai. X-alert video ke liye `_champion_video_try_send` ek
  `champ_final` request likhta hai aur 120s tak finished file ka
  intezar karta hai. Most Shared ke liye `_ms_make_video` ek
  `mostshared` request likhta hai (token list + logo files) aur 240s
  tak intezar karta hai (pehle wala timeout bhi 240s tha). Rank-up video
  ke liye `_rankup_build_video` ek `rankup` request likhta hai (120s).
- **Koi bhi file do processes nahi likhte.** Videobot sirf apni files
  likhta hai (`queue/` read karke delete karta hai, `out/` me likhta
  hai, `heartbeat.json`, aur `$CHAMP_VIDEO_DIR/champ_bases/` me base
  videos). Main bot ki `tracked_calls.json` / `sent_milestones.json` /
  `bot_config.json` ko videobot kabhi nahi likhta, kabhi nahi kholta.
  Videobot tracked calls ko padhta bhi nahi - prerender ke candidates
  main bot khud chun kar request bhejta hai.
- **Videobot ke paas koi Telegram bot token ya userbot session nahi
  hai.** Yeh deliberate hai: main bot hi posting ka malik hai, aur do
  bots ke beech session share karne se ek disconnect ho jata hai (yehi
  masla dropbot ko Nov 24 ko @Wizard_Scan session par hua tha).
- **Agar videobot down hai, main bot rukta nahi.** Har handoff se pehle
  main bot videobot ka heartbeat check karta hai (videobot har 10s me
  `heartbeat.json` likhta hai, render ke doran bhi). Heartbeat stale ho
  to main bot foran fallback karta hai: champion alert normal X media
  se post hota hai, Most Shared text-only post hoti hai, rank-up post
  nahi hoti (aaj fail hone par bhi yehi hota hai). Koi alert kabhi
  videobot ki wajah se atakti nahi.
- **Queue priority:** time-sensitive renders pehle - mostshared, phir
  champ_final, phir rankup, aakhir me champ_base (base speculative hai,
  alert video nahi).
- **Champion bases** usi dir me likhe jate hain jo main bot hamesha se
  use karta hai (`$CHAMP_VIDEO_DIR/champ_bases`, default `/tmp`), is
  liye videobot ki banayi hui base main bot ke read path se byte-compatible
  hai, aur rollback par main bot ki apni prerender bhi unhe use kar sakti
  hai.

## Files (is folder me)

| File | Size | Kya hai |
|---|---|---|
| `videobot.py` | 28,858 B | Poora bot (ek file). Queue loop + prune + heartbeat + 4 render kinds. |
| `apply_videobot_handoff_nov26.py` | 29,919 B | Main bot patch (x_alert.py + most_shared.py + champion_rankup.py, idempotent, .bak backup). |
| `requirements.txt` | 472 B | Python deps (sirf numpy + Pillow; ffmpeg system binary hai). |
| `videobot.env.example` | 543 B | `.env` template - isko `.env` me copy karo. |
| `videobot_test.py` | 27,555 B | Test harness (42 checks, no network, no Telegram). |
| `videobot-test.md` | | Test report (yeh guide ke saath). |
| `videobot-deploy.md` | | Yeh guide. |

**Repo me yeh files bhi dalni hain (live `/opt/bot` se li hui - inke
bina videobot render nahi kar sakta):** `champion_video.py`,
`champion_gauge_neon.py`, `champion_green_trio.py`,
`champion_rocket_neon.py`, `champion_cyberhud_orange.py`,
`most_shared_video.py`, `champion_rankup_video.py` - aur fonts
(`Poppins-Bold.ttf`, `Poppins-BoldItalic.ttf`) agar `/opt/bot` me hon.
**Yeh files workspace me NAHI hain** (Nov 23 sync me nahi aayi thin) -
inhe neeche Step 3 me VPS se seedha copy karna hai, aur repo me bhi
upload kar dena taaki GitHub source bana rahe.

## Videobot kya karta hai / kya NAHI karta

**Karta hai:**
- `champ_base` request par champion base video render karna (gauge
  red 2/5/10, rocket blue 20, cyber orange 30, trio green 40/50,
  trio purple 60, trio green 80/90, trio bw 100+ - wahi routing jo
  main bot aaj use karta hai; 1600x640, 30fps, no audio).
- `champ_final` request par final alert video render karna (base fresh
  ho to warm overlay compose ~12-16s, warna cold render - bilkul waisa
  hi jaisa main bot karta tha).
- `mostshared` request par daily Most Shared video render karna
  (`most_shared_video.build_most_shared_video` se, wahi token list +
  logos jo main bot bhejta hai; 1440x626, 8s).
- `rankup` request par rank-up video render karna
  (`champion_rankup_video.build_rankup_video` se).
- Base files prune karna (max 6, 30 min se purani delete - wahi limits
  jo main bot ki prerender job use karti thi).
- Har 10s me heartbeat likhna, taaki main bot ko pata chale videobot
  zinda hai.

**NAHI karta:**
- Telegram par kuch post nahi karta, koi token/session nahi rakhta.
- Main bot ki data files ko nahi likhta, tracked calls ko nahi padhta.
- Prerender candidates khud nahi chunta - main bot har 60s me
  request bhejta hai (scan + 80% threshold + routing main bot me hi
  rehta hai, wahi code jo aaj chal raha hai).
- Dip / normal X-alert media (GIFs/photos) ko nahi chhedta - woh
  pehle se set file_ids hain, render nahi hote.

## Deploy (ek step, phir ruk kar check karo)

**Pehle: naya repo banao.** GitHub par `realfarhad55-art/videobot`
repo banao (jaise `monbot` aur `droppedcall` banaye thay). Is folder
ki 7 files upload karo: `videobot.py`, `requirements.txt`,
`videobot.env.example`, `apply_videobot_handoff_nov26.py`,
`videobot-deploy.md`, `videobot_test.py`, `videobot-test.md`.

**Step 1 - VPS par folder + venv + files:**

```
mkdir -p /opt/videobot && cd /opt/videobot && python3 -m venv venv && wget -O videobot.py "https://raw.githubusercontent.com/realfarhad55-art/videobot/main/videobot.py" && wget -O requirements.txt "https://raw.githubusercontent.com/realfarhad55-art/videobot/main/requirements.txt" && wget -O videobot.env.example "https://raw.githubusercontent.com/realfarhad55-art/videobot/main/videobot.env.example" && /opt/videobot/venv/bin/pip install -r /opt/videobot/requirements.txt
```

**Step 2 - .env banao (CHAMP_VIDEO_DIR main bot se match karna LAZMI hai):**

```
V=$(grep -oP '^CHAMP_VIDEO_DIR=\K.*' /opt/bot/.env 2>/dev/null || echo /tmp); printf 'CHAMP_VIDEO_DIR=%s\nBOT_DATA_DIR=/opt/bot/data\nVIDEOBOT_DIR=/opt/videobot\n' "$V" > /opt/videobot/.env && cat /opt/videobot/.env
```

Agar upar `CHAMP_VIDEO_DIR=/tmp` dikhe to theek hai (main bot ka
default bhi `/tmp` hai). Agar koi aur value dikhe to wahi use hogi -
dono bots ko ek hi dir dekhni hai, warna base files nahi milengi.

**Step 3 - renderer modules live /opt/bot se copy karo (YEH SAB SE
ZARURI STEP HAI - inke bina videobot render nahi kar sakta):**

```
cp /opt/bot/champion_video.py /opt/bot/champion_gauge_neon.py /opt/bot/champion_green_trio.py /opt/bot/champion_rocket_neon.py /opt/bot/champion_cyberhud_orange.py /opt/videobot/ && ls -la /opt/videobot/*.py
```

Phir optional modules (Most Shared + rank-up ke liye - agar ye files
live `/opt/bot` me na hon to videobot un kinds ko clear error ke saath
fail karega aur main bot fallback karega; champion renders par koi
farq nahi padega):

```
cp /opt/bot/most_shared_video.py /opt/bot/champion_rankup_video.py /opt/videobot/ 2>/dev/null; ls /opt/videobot/most_shared_video.py /opt/videobot/champion_rankup_video.py 2>&1
```

Fonts (agar hon - warna renderers DejaVu fallback use karte hain):

```
ls /opt/bot/*.ttf /opt/bot/assets/fonts/*.ttf 2>/dev/null; cp /opt/bot/*.ttf /opt/videobot/ 2>/dev/null; cp /opt/bot/assets/fonts/*.ttf /opt/videobot/ 2>/dev/null; echo fonts-done
```

**In 7 renderer files ko videobot repo me bhi upload kar do** (GitHub
web UI se), taaki repo poora source rahe.

**Step 4 - self-test chalao (pipeline proof, ~30-60s lagega):**

```
cd /opt/videobot && /opt/videobot/venv/bin/python videobot.py --check
```

`CHECK OK: gauge red 2x base rendered in ...s, ... bytes` aana
chahiye. Agar `CHECK FAIL` aaye to aage mat badho - screenshot bhejo.

**Step 5 - systemd service banao + start karo:**

```
printf '[Unit]\nDescription=WizardScan video-generation bot (videobot)\nAfter=network.target\n\n[Service]\nType=simple\nUser=root\nWorkingDirectory=/opt/videobot\nExecStart=/opt/videobot/venv/bin/python videobot.py\nRestart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/videobot.service && systemctl daemon-reload && systemctl enable videobot.service && systemctl start videobot.service && systemctl is-active videobot.service
```

**Step 6 - heartbeat check karo (videobot zinda hai?):**

```
sleep 5 && cat /opt/videobot/heartbeat.json && journalctl -u videobot.service --since "2 minutes ago" --no-pager | tail -10
```

Heartbeat JSON me `"renderers"` me `champion_video`, `champion_green_trio`,
`champion_rocket_neon`, `champion_cyberhud_orange` = `true` hone chahiye.
`most_shared_video` / `champion_rankup_video` = `false` ho to Step 3
wapas check karo (sirf un kinds par farq padega).

**Step 7 - main bot patch (VIDEOBOT PEHLE start karo, phir yeh):**

```
cd /opt/bot && wget -O apply_videobot_handoff_nov26.py "https://raw.githubusercontent.com/realfarhad55-art/videobot/main/apply_videobot_handoff_nov26.py" && python3 apply_videobot_handoff_nov26.py /opt/bot && systemctl restart bot.service
```

`E1 OK` se `E8 OK` tak sab aane chahiye, phir `DONE`. Agar koi `FAIL`
aaye to patch ne kuch nahi likha (fail-safe) - screenshot bhejo, force
mat karo.

**Step 8 - verify karo:**

```
sleep 90 && ls -lat /tmp/champ_bases/ | head -8
```

(Upar `/tmp` ki jagah wohi dir use karo jo Step 2 me
`CHAMP_VIDEO_DIR` dikhi thi.) 90 second me kam se kam ek fresh
`champ-base-*.mp4` file nazar aani chahiye - yeh videobot ki banayi
hui base hai. Phir:

```
journalctl -u videobot.service --since "10 minutes ago" --no-pager | grep -a "render" | tail -10
```

`render done: champ_base ...` lines aani chahiye. Aur main bot ke
logs me:

```
journalctl -u bot.service --since "10 minutes ago" --no-pager | grep -a "videobot" | tail -10
```

`videobot: champion base requested ...` lines aani chahiye. Main bot
ka CPU bhi check karo - videobot ke load uthane ke baad neeche aana
chahiye:

```
ps aux --sort=-%cpu | head -6
```

**Step 9 - Most Shared test (agle roz 02:00 PKT se pehle):**

```
/mostsharedtest
```

Bot ko DM me preview bhejna chahiye **video ke saath**. Agar preview
text-only aaye to `journalctl -u videobot.service` me `mostshared`
wale render ka error dekho aur screenshot bhejo.

## Rollback (agar kuch galat ho)

Main bot wapas apna khud ka render karne lagega, videobot band:

```
cp /opt/bot/x_alert.py.bak_videobot_nov26 /opt/bot/x_alert.py && cp /opt/bot/most_shared.py.bak_videobot_nov26 /opt/bot/most_shared.py && cp /opt/bot/champion_rankup.py.bak_videobot_nov26 /opt/bot/champion_rankup.py && systemctl restart bot.service && systemctl stop videobot.service
```

Phir check karo: `systemctl is-active bot.service` = `active`, aur
champion prerender wapas main bot par chalne lagega. Videobot ki
banayi hui base files (`champ_bases/` me) main bot bhi use kar sakta
hai - delete karne ki zarurat nahi.

## Coordination note (xalertbot + dipbot parallel me ban rahe hain)

Videobot ka handoff patch `x_alert.py` ko bhi edit karta hai
(prerender job + `_champion_video_try_send`). xalertbot ka patch bhi
`x_alert.py` ko edit karega. Dono alag regions ko chhedte hain, lekin
**patch lagane se pehle** dusre patch ka status check karo: agar
xalertbot ka patch `_champion_video_try_send` ya
`champion_prerender_job` ko badal chuka hai, to videobot patch ka
anchor fail ho sakta hai - aise me force mat karo, pehle batana.
Videobot `sent_milestones.json`, `tracked_calls.json`, monitoring,
dip ya forwarding ko nahi chhedta, is liye un cheezon par koi
shared-write conflict nahi hai.

## Known gaps / jo verify nahi ho saka

- **Live-file anchors:** patch Nov 23 base copies par verify hua hai.
  Live `/opt/bot` files me Nov 24/25 ke patches hain (Claude AI ka dip
  patch bhi), lekin jin regions ko yeh patch chhedta hai
  (`champion_prerender_job`, `_champion_video_try_send`,
  `_ms_make_video`, `_rankup_build_video`) unhe Nov 24/25 ke patches
  ne nahi chheda (record ke mutabiq). Phir bhi deploy par anchor fail
  ho to patch kuch nahi likhega - yeh fail-safe hai, force mat karna.
- **`most_shared_video.py` + `champion_rankup_video.py`:** yeh dono
  files workspace me kabhi synced nahi hui thin (VPS-only). Videobot
  inhe optional import karta hai; inke exact function signatures wohi
  hain jo main bot use karta hai (`build_most_shared_video(tokens)`
  jo path return karta hai; `build_rankup_video(**kw)`). Agar live
  module ka signature alag nikla to render clear error ke saath fail
  hoga aur main bot fallback karega - champion renders par farq nahi
  padega.
- **Live render proof:** gauge base render + warm final compose real
  code se prove hua hai (test report dekho). Trio/rocket/cyber base
  renders bhi real code se prove hue hain. Most Shared aur rank-up
  renders real modules se is sandbox me prove nahi ho sake (modules
  workspace me hain hi nahi) - inka pehla live render hi asli proof
  hoga; Step 9 ka `/mostsharedtest` usi ke liye hai.
- **Timing:** is sandbox (1.5 shared CPU) me gauge base render me
  ~33s laga; VPS (8 vCPU) par tez hona chahiye (pehle ~8-10s tha).
  Videobot ke timeouts (base 300s, final 180s, mostshared 300s,
  rankup 180s) isi hisaab se rakhe gaye hain.
