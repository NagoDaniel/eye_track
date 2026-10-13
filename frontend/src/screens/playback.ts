// Playback screen (PLAN §5.7): the stimulus fills the viewport letterboxed on black, no controls,
// no seeking; the space bar pauses only if `allow_pause`. Leaving fullscreen pauses (PLAN §5.2).

import { contentRect } from "../calibration/points";
import type { StudyConfig, VideoRef } from "../config";
import { PAUSE_KEY } from "../constants";
import type { EventLog, Size } from "../eventLog";
import { fullscreenGate, isSeekAttempt } from "../playbackGate";
import type { SessionEvents } from "../sessionEvents";
import { button, el, p } from "../ui/dom";

export interface PlaybackDeps {
  doc?: Document;
  win?: Window;
}

/** Resolves when the video has ended. Rejects if it cannot be loaded or played. */
export function playbackScreen(
  root: HTMLElement,
  video: VideoRef,
  objectUrl: string,
  playback: StudyConfig["playback"],
  log: EventLog,
  events: Pick<SessionEvents, "setVideoIntrinsic">,
  deps: PlaybackDeps = {},
): Promise<void> {
  const doc = deps.doc ?? document;
  const win = deps.win ?? window;
  return new Promise((resolve, reject) => {
    const media = doc.createElement("video");
    media.src = objectUrl;
    media.controls = false;
    media.playsInline = true;
    media.preload = "auto";
    media.disablePictureInPicture = true;

    const overlay = el(
      "div",
      { class: "player-overlay" },
      p("Return to fullscreen to continue"),
      button("Enter fullscreen", () => {
        doc.documentElement.requestFullscreen().catch((e: unknown) => reject(e));
      }),
    );
    overlay.hidden = true;
    root.replaceChildren(el("div", { class: "player" }, media, overlay));

    let heldForFullscreen = false;
    let lastGoodTime = 0;
    const mediaTime = (): number => media.currentTime;
    const play = (): void => {
      media.play().catch((e: unknown) => {
        // pause() right after play() rejects the play promise with AbortError; that is not a failure
        if (!(e instanceof Error && e.name === "AbortError")) reject(e);
      });
    };

    /** Returns true if the video is being held back. */
    const applyFullscreenGate = (): boolean => {
      const result = fullscreenGate({
        requireFullscreen: playback.require_fullscreen,
        isFullscreen: doc.fullscreenElement !== null,
        pausedForFullscreen: heldForFullscreen,
      });
      heldForFullscreen = result.pausedForFullscreen;
      if (result.action === "pause") media.pause();
      if (result.action === "resume") play();
      overlay.hidden = !heldForFullscreen;
      return heldForFullscreen;
    };

    const listeners: [EventTarget, string, EventListener][] = [];
    const on = (target: EventTarget, type: string, handler: EventListener): void => {
      target.addEventListener(type, handler);
      listeners.push([target, type, handler]);
    };
    const finish = (): void => {
      for (const [target, type, handler] of listeners) target.removeEventListener(type, handler);
      events.setVideoIntrinsic(null);
      media.removeAttribute("src");
      resolve();
    };

    on(media, "loadedmetadata", () => {
      const size: Size = { width: media.videoWidth, height: media.videoHeight };
      const viewport: Size = { width: win.innerWidth, height: win.innerHeight };
      events.setVideoIntrinsic(size);
      log.log("video_load", {
        video_id: video.id,
        duration: media.duration,
        width: size.width,
        height: size.height,
        rect: contentRect(viewport, size.width, size.height),
      });
      if (!applyFullscreenGate()) play();
    });
    on(media, "play", () => log.log("video_play", { video_id: video.id, media_time: mediaTime() }));
    on(media, "pause", () => {
      if (!media.ended) log.log("video_pause", { video_id: video.id, media_time: mediaTime() });
    });
    on(media, "waiting", () => log.log("video_stall", { video_id: video.id, media_time: mediaTime() }));
    on(media, "ended", () => {
      log.log("video_ended", { video_id: video.id });
      finish();
    });
    on(media, "error", () => reject(new Error(`Video ${video.id} could not be played`)));
    on(media, "timeupdate", () => {
      if (!media.seeking) lastGoodTime = media.currentTime;
    });
    on(media, "seeking", () => {
      if (isSeekAttempt(media.currentTime, lastGoodTime)) media.currentTime = lastGoodTime;
    });
    on(doc, "fullscreenchange", () => {
      applyFullscreenGate();
    });
    on(doc, "keydown", (event) => {
      if ((event as KeyboardEvent).key !== PAUSE_KEY) return;
      event.preventDefault();
      if (!playback.allow_pause || heldForFullscreen) return;
      if (media.paused) play();
      else media.pause();
    });
  });
}
