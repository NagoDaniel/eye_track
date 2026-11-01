# Running a study

A guide for the researcher: what to do before the first participant, during the study, and after it. How to install the server, back it up and use every admin screen is in [`README.md`](../README.md); what every stored file holds is in [`DATA_FORMATS.md`](DATA_FORMATS.md). This page is the order of work and the things that went wrong in the tests so far.

Commands run on the server, in the project folder, as `docker compose exec api python cli.py <command>`.

## 1. What to expect from the data

Read this first; it decides how you design the study.

- **Accuracy is coarse.** In the pilot recordings (twelve sessions with two different webcams on laptop screens) the gaze was typically **140 to 290 px away from where the person was really looking** on a 1280 x 720 picture, that is 5 to 10 % of the screen diagonal. It is worse vertically than horizontally, and it is pulled towards the centre of the screen (the corners are too close to the middle). The gaze lags the video by about 40 to 300 ms. Numbers and method: [`PROGRESS.md`](PROGRESS.md), "H1", "H2" and "H3 result".
- **So areas of interest must be large.** An AOI as small as 65 px caught 4 % of the time that a participant spent looking at it. With the analysis option *AOI tolerance k* of 2 it caught about 60 %. Draw AOIs at least two to three times the accuracy (a third of the picture width is a good size for "left side" and "right side" questions, not for single words or buttons), keep neighbours apart, and use *AOI tolerance k* 1 or 2 in the analysis.
- **Heatmaps and synchrony are fine** for questions like "where does the group look when this happens"; they are not fine for "did this person read this line".
- **Glasses** do not stop it working (the pilot had sessions with and without), but a constant offset between the validation blocks is more common with them. The analysis uses drift-corrected gaze by default for that reason.
- **Pipeline A is the one to use.** Pipeline B (the gaze network) was not better than A on the same recordings and takes about twice the recording length of CPU time per session. Run it only if you want to compare. Do not mix the two in one analysis.
- **One session is not enough to see a problem; six are.** Look at the quality of every session (below), not only at the heatmap.

## 2. Before the study

1. **Install and secure the server** as in the README (section 1). Put `DATA_DIR` on an **encrypted volume**: the folder holds the participants' webcam recordings and the application does not encrypt anything. Keep `.env` (the secrets) outside the backups.
2. **Back up and test the restore** (README, "Backup and restore") before the first real session. Do the restore test once.
3. **Ethics and consent.** The consent screen shows `backend/app/consent/<version>.txt`; `v1` is a placeholder. Replace it with the text your ethics committee approved (it must state that webcam video is recorded and stored on a university server, the purpose, the retention period and how to withdraw), change `consent_version` in the study, and rebuild (`docker compose up -d --build`). The application does not delete data by itself and has no withdrawal button yet: if a participant withdraws, their sessions have to be removed by hand (database rows and the session folder under `studies/<id>/sessions/`). Agree the retention period with the ethics committee with this in mind.
4. **Stimulus videos.** Upload them under *Videos* (H.264, constant frame rate; other files are accepted with a warning). Draw the AOIs in the AOI editor on the video page, large (section 1), with the times when each area exists.
5. **Create the study** (*Studies*): the playlist, and the settings you want to change. Defaults worth knowing: `playback.allow_pause` is off, the camera check needs a face for 3 s, the calibration has 13 dots and the validation 5, `calibration_gate.on_fail` is `continue_flagged` (a bad calibration is repeated up to 3 times, then the session goes on flagged), `inclusion.min_valid_fraction` 0.7 decides whether a session counts (`inclusion.max_validation_error_norm` 0.12 only applies when the validation dots do not train the gaze model; by default they do, see section 5). Debug mode must be **off** (`debug_allowed` false).
6. **Check end to end on real hardware: the moving-dot test.** The repository has a video of a dot that moves along a known path (`e2e/fixtures/moving_dot.mp4`, 21.8 s, 1280 x 720; its path is in `moving_dot_path.json`). Upload it as a video, make a study that plays it, open the participant link on the laptop and the camera you will use, **follow the dot with your eyes** for the whole video, wait until the session is *processed*, then run the report on the session:

   ```
   docker compose exec api python cli.py moving-dot-report <session id> --plot /data/dot.png
   ```

   It prints the median distance between your gaze and the dot (it passes at 0.15 of the screen diagonal or less, with at least 80 % valid frames; exit status 0 passes, 2 fails), separately for the pauses and the movements, the lag, and with `--plot` a picture of path against gaze. Any session of that study will do; with debug mode on (the study's button *Turn debug mode on and show the debug link*) you also get the live panel, but turn it off again before real participants. If the test fails, look at the plots on the session page and at the section "When something looks wrong" below before you recruit anybody.
7. **Try the participant's path** yourself once without debug mode, on the least powerful laptop that a participant might use (section 5, *Slow computers*).

## 3. Telling the participants

The participant opens `https://<your domain>/s/<study code>` (add `?p=<code>` to prefill their code). They need:

- **Desktop or laptop Chrome or Edge**, current version. Firefox may work (not tested yet); Safari, phones and tablets are not supported and the page says so.
- A webcam, **a lit face** (a window or lamp in front of them, not behind), the whole face in view, a normal distance (an arm's length), a quiet 5 to 15 minutes, and **no other program using the camera**.
- To **keep their head fairly still and look at the screen** during the dots and the videos: the gaze is only as good as the calibration, and a participant who shifts a lot between the start and the end leaves a large drift.
- To allow the camera when the browser asks, and to stay in fullscreen (leaving it is logged and flagged).

Tell them what is recorded and how to withdraw, as in the consent text.

## 4. During the study

- **Diagnostics** (admin, *Diagnostics*): is the worker alive (the heartbeat), how many jobs wait or failed, how much disk is free, which browsers and cameras the last sessions used. Check it at the start and end of each day.
- **Sessions** (admin): after each session the worker processes it by itself. A session shows *processed* when the quality report exists. Open the first sessions yourself and read them (section 5).
- **Disk.** Count 150 MB per 10-minute session for raw and derived data. Watch the free space in *Diagnostics*; there is no quota, so a full disk stops uploads.
- **Updating the server during a study** (`git pull && docker compose up -d --build`) restarts the worker: a running job is picked up again, but do it between sessions.

## 5. After a session: is it usable?

Open the session in the admin. The **quality report** says whether the session is *included* in the analysis and why not:

| Reason | Meaning and what to do |
|---|---|
| `calibration_failed` | the calibration did not produce a usable model (for example too few frames with a face); nothing to analyse, the participant would have to repeat the session |
| `valid_fraction_low` | less than `min_valid_fraction` of the video time has valid gaze (face lost, blinks, looking away) |
| `validation_error_high` | only when the validation dots do not train the model (`calib_fit_validation` off): the validation dots were predicted more than `max_validation_error_norm` of the diagonal away. By default (on) this reason never appears: the validation error is reported, held out, but does not exclude anybody, so look at it yourself (below) |
| `no_playback_data`, `no_validation_data` | the video part or the validation part is missing (the participant left early) |
| `debug_session` | made in debug mode; left out by default |

Flags (shown next to the session, none excludes it):

- `calibration_gate_failed`: the participant's own calibration check failed in the browser; the session continued flagged.
- `sync_*`: the webcam and the video were hard to align in time (`sync_short`: a recording shorter than 2 minutes, normal for short videos; `sync_weak_peak` or `sync_residual_high`: look at the sync plot).
- `stimulus_frames_dropped`: the browser could not show more than 10 % of the video's frames in time (a slow computer). The session is kept, but its timing is less exact.
- `interrupted`: the page was reloaded during the session.

In the pilot recordings the validation error was between 0.03 and 0.10 of the diagonal. Since the validation dots also train the gaze model (DECISIONS 2026-11-01), the error shown is measured with a model fitted without each block, and it no longer excludes a session: **a participant who did not look at the dots can be included** as long as the face was tracked, so check the validation error of every session yourself; above about 0.12 of the diagonal the gaze is not to be trusted. Record real participants **without debug mode**: debug sessions are excluded from every analysis. The **plots** on the session page show where the problem is: `calibration.png` (targets against predictions, they should be near their dots), `validation.png` (error per block and the drift between them), `gaze.png` (the gaze over time) and `sync.png`. The **debug replay** (button on the session page) plays the video with the participant's gaze drawn on it; it contains the participant's webcam picture and is for admins only.

### Slow computers

Dropped video frames are counted and flagged (above), never a reason to refuse a participant. What the simulation shows (the browser's CPU slowed down 4 and 6 times, a 20 s 720p video): at most about 1 % of the frames dropped, no stalls, no lost face. A real low-end laptop may behave differently (its video decoder, thermal limits, other programs); `make e2e-throttle` repeats the simulation, and the *Diagnostics* page and the flag tell you what happened to real participants.

## 6. Analysis

In *Analysis* (README, "Running a study", points 6 to 8): choose the study and the video, keep *Gaze: drift-corrected* and the Pipeline A, set *AOI tolerance k* as in section 1, and start. The run page shows which sessions were included and which were left out with the reason, and gives a ZIP with the heatmap video, the heatmap pictures per time segment, the AOI metrics as CSV, the attentional synchrony and `run.json` (parameters, participants, code version). Keep `run.json` with the results: it is what makes them reproducible. The analysis can be run again at any time with other AOIs or options; every run is kept.

## 7. Moving and keeping the data

- **Export a whole study** (button on the study page, or `cli.py export-study <study id>`): one ZIP with the settings, videos, sessions (raw and derived) and analyses. It contains the participants' webcam video: treat it as personal data, keep it encrypted and delete it when it has done its job. `--no-raw-video` leaves the webcam video out.
- **One session**: `cli.py export-session <session id>` and `cli.py import-session` on another machine, for example to look at a problem on your own laptop.
- **Backups**: `./scripts/backup.sh <folder>` (and `--no-raw-video`), restore with `./scripts/restore.sh`. See the README.

## 8. Commands you will use

| Task | Command |
|---|---|
| Make an admin account | `cli.py create-admin --username <name>` |
| Process a session again (all or from a stage) | `cli.py process <session id> [--from-stage sync]` |
| Run the pilot check on a session | `cli.py moving-dot-report <session id> --plot /data/dot.png` |
| Render the debug replay | `cli.py render-debug <session id>` |
| Analyse one video | `cli.py analyze <study id> <video id> [--tolerance-k 1] [--as-measured] [--dots]` |
| Pipeline B on one session or a study | `cli.py process-b <session id>`, `cli.py process-b --study <study id>` |
| Compare the two pipelines | `cli.py compare <study id>` |
| Export and import a study | `cli.py export-study <study id>`, `cli.py import-study <file>` |
| Export and import a session | `cli.py export-session <session id>`, `cli.py import-session <file>` |

Status, logs and updates are in the README, "Operating it".

## 9. When something looks wrong

| Symptom | Look at |
|---|---|
| The participant cannot start | the message on the first screen (unsupported browser, camera blocked); `docker compose logs proxy api` for a certificate or network problem |
| Sessions stay *uploaded* | *Diagnostics*: is the worker alive; `docker compose logs worker` |
| A session is *failed* | its page: the job's log names the stage; run it again from there with the button, or `cli.py process <id> --from-stage <stage>` |
| Gaze is far from where the person looked | the calibration plot (a participant who moved after the dots, glasses reflections, a dark face); the validation plot for drift; ask the participant to repeat in better light |
| Every session of a day is poor | the room: backlight, a monitor on one side, the camera on top of a second screen |
| The heatmap is shifted away from the picture | the drift-corrected default is on; check the validation drift in the plots; larger AOIs and *AOI tolerance* |
| A job runs for hours | *Studies*, the study's *Jobs* section: cancel it; Pipeline B is the long one |

If it still does not make sense, `cli.py export-session <id>` gives a ZIP that reproduces the problem on a development machine.
