// Bingo number-calling voice announcer. Plays a pre-generated audio clip
// ("B! ሰባት!") whenever a number is called, via a small FIFO queue so
// rapid calls never overlap or interrupt each other. See
// web/miniapp/audio/calls/README.md for the audio-file contract this
// module reads from.
//
// No speech-synthesis fallback on purpose: browser speechSynthesis
// Amharic support is inconsistent-to-absent on real devices, and a
// mispronounced/garbled fallback would be worse than the silent
// skip-with-warning this module already does when a clip is missing.
// Every public method is wrapped so an audio failure (missing file,
// unsupported format, autoplay block) never breaks gameplay.

const AUDIO_BASE = "/audio/calls";
const warnedMissing = new Set();

function pad(n) {
  return String(n).padStart(2, "0");
}

function clipUrl(letter, number) {
  return `${AUDIO_BASE}/${letter}_${pad(number)}.mp3`;
}

class VoiceCaller {
  constructor() {
    this._queue = [];
    this._announced = new Set();
    this._playing = false;
    this._enabled = true;
    this._volume = 1;
    this._speed = 1;
    this._unlocked = false;
    this._lastCall = null;
  }

  setEnabled(enabled) {
    this._enabled = !!enabled;
    if (!this._enabled) this._queue = [];
  }

  isEnabled() {
    return this._enabled;
  }

  setVolume(volume) {
    this._volume = Math.min(1, Math.max(0, volume));
  }

  getVolume() {
    return this._volume;
  }

  setSpeed(speed) {
    this._speed = Math.min(2, Math.max(0.5, speed));
  }

  getSpeed() {
    return this._speed;
  }

  // Resets per-round dedup state -- call on room join / new round, the
  // same lifecycle point the "called" number set itself resets at.
  resetRound() {
    this._announced.clear();
    this._queue = [];
  }

  // Must be invoked from a real user-gesture handler (the room-join
  // click) so later programmatic .play() calls aren't blocked by mobile
  // Safari / Telegram WebView autoplay policy.
  unlock() {
    if (this._unlocked) return;
    this._unlocked = true;
    try {
      const audio = new Audio();
      audio.muted = true;
      const p = audio.play();
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch {
      // Autoplay unlock is best-effort; a failure here just means the
      // first real announcement may need another gesture, not a bug.
    }
  }

  announce(letter, number, callIndex) {
    if (!this._enabled) return;
    if (callIndex !== undefined && callIndex !== null) {
      if (this._announced.has(callIndex)) return;
      this._announced.add(callIndex);
    }
    this._queue.push({ letter, number });
    this._pump();
  }

  // Bypasses dedup on purpose -- an explicit replay request, not a
  // duplicate broadcast.
  replayLast() {
    if (!this._enabled || !this._lastCall) return;
    this._queue.push(this._lastCall);
    this._pump();
  }

  _pump() {
    if (this._playing || this._queue.length === 0) return;
    const call = this._queue.shift();
    this._playing = true;
    try {
      const audio = new Audio(clipUrl(call.letter, call.number));
      audio.volume = this._volume;
      audio.playbackRate = this._speed;
      const advance = () => {
        this._playing = false;
        this._pump();
      };
      audio.onended = advance;
      audio.onerror = () => {
        const key = `${call.letter}${call.number}`;
        if (!warnedMissing.has(key)) {
          warnedMissing.add(key);
          console.warn(`[voice] missing or unplayable audio clip for ${key}`);
        }
        advance();
      };
      const playPromise = audio.play();
      if (playPromise && typeof playPromise.catch === "function") {
        playPromise.catch(() => advance());
      }
      this._lastCall = call;
    } catch {
      this._playing = false;
      this._pump();
    }
  }
}

export const voiceCaller = new VoiceCaller();
