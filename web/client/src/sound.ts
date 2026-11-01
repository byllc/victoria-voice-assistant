let audio: AudioContext | null = null;

// No chime file is served yet; a short two-tone beep stands in for it.
export function beep(): void {
  try {
    audio ??= new AudioContext();
    const start = audio.currentTime;
    for (const [offset, frequency] of [[0, 880], [0.25, 660]] as const) {
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.2, start + offset);
      gain.gain.exponentialRampToValueAtTime(0.001, start + offset + 0.22);
      oscillator.connect(gain).connect(audio.destination);
      oscillator.start(start + offset);
      oscillator.stop(start + offset + 0.22);
    }
  } catch {
    // Audio may be blocked until the page has had a user gesture; the banner still shows.
  }
}
