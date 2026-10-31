import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

type Mode = "hands-free" | "push-to-talk";

export interface VoiceUi {
  onUserText(text: string): void;
  onBotText(text: string, newTurn: boolean): void;
  onServerMessage(message: unknown): void;
  setState(text: string): void;
}

/**
 * Browser voice: SmallWebRTC to the assistant service (signaling proxied at /api/offer,
 * audio direct). Turn detection runs on the server; push-to-talk just gates the mic.
 */
export class Voice {
  private client: PipecatClient | null = null;
  private mode: Mode = "hands-free";
  private botTurnOpen = false;

  constructor(
    private readonly ui: VoiceUi,
    private readonly audio: HTMLAudioElement,
  ) {}

  get connected(): boolean {
    return this.client !== null;
  }

  static unavailableReason(): string | null {
    if (!window.isSecureContext) return "Voice needs HTTPS (or localhost); see docs/https.md.";
    if (!navigator.mediaDevices?.getUserMedia) return "This browser has no microphone access.";
    return null;
  }

  async start(mode: Mode): Promise<void> {
    this.mode = mode;
    const client = new PipecatClient({
      transport: new SmallWebRTCTransport(),
      enableMic: true,
      enableCam: false,
      callbacks: {
        onConnected: () => this.ui.setState("connecting…"),
        onBotReady: () => {
          this.setMic(this.mode === "hands-free");
          this.ui.setState(this.mode === "hands-free" ? "listening" : "hold to talk");
        },
        onDisconnected: () => this.stopped("voice off"),
        onUserStartedSpeaking: () => this.ui.setState("hearing you…"),
        onUserStoppedSpeaking: () => this.ui.setState("thinking…"),
        onBotStartedSpeaking: () => this.ui.setState("speaking"),
        onBotStoppedSpeaking: () => {
          this.botTurnOpen = false;
          this.ui.setState(this.mode === "hands-free" ? "listening" : "hold to talk");
        },
        onUserTranscript: (data) => {
          if (data.final && data.text.trim()) {
            this.botTurnOpen = false;
            this.ui.onUserText(data.text.trim());
          }
        },
        onBotOutput: (data) => {
          // Sentence-level text only; word-level updates would repeat it.
          if (data.text_type && data.text_type !== "sentence") return;
          if (data.spoken_status && data.spoken_status !== "new") return;
          this.ui.onBotText(data.text, !this.botTurnOpen);
          this.botTurnOpen = true;
        },
        onServerMessage: (data) => this.ui.onServerMessage(data),
        onTrackStarted: (track, participant) => {
          if (track.kind === "audio" && participant && !participant.local) {
            this.audio.srcObject = new MediaStream([track]);
            void this.audio.play().catch(() => undefined);
          }
        },
        onError: (message) => this.ui.setState(`voice error: ${JSON.stringify(message.data ?? message)}`),
      },
    });
    this.client = client;
    this.ui.setState("starting…");
    try {
      await client.connect({ webrtcRequestParams: { endpoint: "/api/offer" } });
    } catch (error) {
      this.stopped(`voice unavailable: ${error instanceof Error ? error.message : String(error)}`);
      throw error;
    }
  }

  async stop(): Promise<void> {
    const client = this.client;
    this.client = null;
    await client?.disconnect();
    this.stopped("voice off");
  }

  setMode(mode: Mode): void {
    this.mode = mode;
    this.setMic(mode === "hands-free");
    this.ui.setState(mode === "hands-free" ? "listening" : "hold to talk");
  }

  setMic(enabled: boolean): void {
    this.client?.enableMic(enabled);
  }

  private stopped(state: string): void {
    this.client = null;
    this.audio.srcObject = null;
    this.ui.setState(state);
  }
}
