# T13 recordings: the long moving-dot study

What to record before the next tuning steps, how to behave while recording, and how to label the sessions. Written 2026-10-31 for the human; background in `docs/PROGRESS.md` (T13, T14) and `backend/lab/README.md`.

**Why:** every result so far comes from 10 sessions of one person on a 22 s video. Two questions need new data: does fitting on the validation dots (T14) still help when the video is long, and do the results hold for another person.

## What you will record

One new study that plays **two moving-dot videos of 1 min 54 s each** back to back (`backend/lab/videos/long_dot_1.mp4` and `long_dot_2.mp4`). The app shows a validation block before the first video, **between the two** (the middle of the 4 minutes) and after the second. Each video has:

- 40 rests of 1.5 s on a 5 x 4 grid (10 % to 90 % of the screen, so the far corners and four rows), each joined by a 0.7 s jump;
- then **smooth pursuit**: a slow figure-eight through the centre (10 s) and one slow turn around an ellipse (8 s);
- a rest at the centre at the start and the end.

One session takes about 8 to 10 minutes: camera check, calibration (13 dots), validation (5), video 1, validation, video 2, validation.

**Sessions:**

| Who | Sessions | Codes |
|---|---|---|
| You | 4: two without glasses, two with | `DA0101`, `DB0202`, `DC0101`, `DD0202` |
| The other person | 2 (both as they normally are: with glasses if they wear them) | `PA0101` or `PA0202`, `PB...` |
| Optional | 1 more on another webcam or in other light, either person | next letter, e.g. `DE0101` |

Leave at least 10 minutes between two sessions of the same person (tired eyes are a different condition), and do them on at least two different days if you can.

## Setting up (once)

1. Start the local stack the way you did for study 7 (Docker Desktop on, then `docker compose up -d`), and log in to the admin.
2. **Videos:** upload `backend/lab/videos/long_dot_1.mp4` with the title `Long dot 1`, then `long_dot_2.mp4` with the title `Long dot 2`. Upload these files as they are (do not re-encode them): the lab recognises them by their exact bytes.
3. **Study:** create a study named `T13 long dot` with the playlist **Long dot 1, then Long dot 2**. Keep every other setting at the default, as in study 7: validation before, between and after on; calibration gate on; fullscreen required; recording 1280 x 720 at 30 fps; debug mode off. Note the study's id.
4. **Same equipment as study 7** for your four sessions: the same laptop, screen and webcam, the browser in fullscreen (Chrome).

**A first check:** do one of your sessions, wait until it shows *processed* in the admin, then run, from the repository root:

```
make lab ARGS="import-study <study id>"
make lab ARGS="reference <study id>"
```

The import must say `dot videos ['long_dot_1', 'long_dot_2']`, and the report must show two rows for the session (`L1` and `L2`). If it says `no dot video`, the uploaded files are not the generated ones: tell me before recording more.

## How to behave while recording

Read this part to the other person too.

**Sitting:**
- Sit as you would to watch a lecture on this laptop: comfortable, an arm's length from the screen, face lit from the front (a window or lamp in front of you, not behind).
- Keep your head **naturally still**: do not freeze or brace, and do not lean far in or out or turn your head on purpose. Small natural movements are fine and wanted; they are what the model has to cope with.
- Stay seated through the whole session, including the validation between the two videos.
- If you wear glasses in that session, wear them from the camera check to the end; do not take them off or push them up.

**Calibration and validation dots:**
- Look at the **centre of the dot** as soon as it appears and keep looking at it.
- Click when it has shrunk, **while still looking at the dot**, not at the mouse pointer. The 0.7 s before your click are the samples the model learns from.
- If the app asks you to repeat the calibration, just do it again; note it.

**During the two videos:**
- **Follow the dot with your eyes only**, always looking at the dark centre.
- When it jumps, follow it straight away; do not guess where it will go next.
- During the figure-eight and the ellipse, **track it smoothly**, staying on it.
- Blink normally. Do not look away. If you do look away, lose the dot or get distracted, note roughly when (for example "about 1 minute into video 2").

**Do not:** talk, use the phone, rest your chin on your hand, or move the laptop during a session.

## Labelling

**Participant code** (the app asks for it; 2 letters and 4 digits):
- first letter, the person: `D` for you, `P` for the other person (a second extra person: `Q`);
- second letter, the session: `A`, `B`, `C`, ... in the order recorded, so every session has its own code;
- digits, the condition: `0101` without glasses, `0202` with glasses (as in studies 6 and 7).

**The labels file:** after each session add one line to `lab_data/labels.csv` (create it with exactly this first line). `lab_data/` is gitignored and never committed:

```
study,code,person,glasses,webcam,notes
8,DA0101,P1,no,built-in,"evening, ceiling light; calibration repeated once"
8,DB0202,P1,yes,built-in,"morning, window in front"
8,PA0101,P2,no,built-in,"looked away about 40 s into video 2"
```

- `person`: `P1` (you), `P2` (the other person): no names.
- `glasses`: `yes` or `no`.
- `webcam`: `built-in`, or the external camera's model.
- `notes`: time of day, light, anything that went wrong (calibration repeated, lost the dot, fullscreen left, a noise that distracted you).

The lab prints these labels next to each session in every report.

## What to look out for

- The second video starts after the validation block; if the app goes straight to the end after video 1, the playlist is wrong.
- Calibration repeated more than once, face-lost or "stay in fullscreen" messages: note them.
- After processing, the session page: status *processed* (not *failed*), the quality report (included or not, and why), and the dropped-frames row. Send me any *failed* reason.
- The video should play smoothly; if it stutters, note it (other programs open, power saver).

## The other person: consent and their data

Their face is recorded and stored on your machine. Before the session, tell them what is recorded (webcam video of their face, where they look, for testing the eye tracker in your thesis project), that it stays on your computer and is never published or committed, and that you will delete it whenever they ask. Get their OK (in writing if your university requires it for thesis data). To delete their data later: remove their session folders under `data/studies/<id>/sessions/` and `lab_data/sessions/studies/<id>/sessions/`, and the session rows in the admin database.

## What to send me

- The study id.
- The finished `lab_data/labels.csv` (or its lines).
- Anything unusual from the list above.

Then I import the study into the lab and run, in this order:
1. the baseline on the long videos (per video and for the pursuit segments);
2. **the honest check of T14**: fit on the calibration plus the validation dots before and between the videos, and score video 2 (`fit_validation = "before_and_between"`), against fitting on all dots;
3. whether the earlier results (`f-no-face`, `v-all`) hold for the new sessions and for the other person.
