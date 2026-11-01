import { useEffect, useRef, useState } from "react";

import { RTVIEvent } from "@pipecat-ai/client-js";
import { usePipecatClientMicControl, usePipecatClientTransportState, useRTVIClientEvent } from "@pipecat-ai/client-react";
import { Button, ConnectButton, TextInputComponent, UserAudioControl } from "@pipecat-ai/voice-ui-kit";

import type { VoiceHandlers } from "./App";
import { NotificationCard } from "./NotificationCard";
import type { Chat } from "./useChat";
import type { ServerMessage } from "./types";

type Mode = "hands-free" | "push-to-talk";

function voiceUnavailable(): string | null {
  if (!window.isSecureContext) return "Voice needs the HTTPS address (docs/https.md).";
  if (!navigator.mediaDevices?.getUserMedia) return "This browser has no microphone access.";
  return null;
}

export function ChatView({ chat, voice }: { chat: Chat; voice: VoiceHandlers }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" }); // returns a promise in newer browsers: not an effect cleanup
  }, [chat.log]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="min-h-0 flex-1 overflow-y-auto pr-1" aria-live="polite">
        {chat.log.length === 0 && (
          <p className="py-8 text-center text-sm opacity-60">Type a message, or start voice and just talk.</p>
        )}
        <ul className="flex flex-col gap-2">
          {chat.log.map((entry) => (
            <li key={entry.id}>
              {entry.kind === "you" ? (
                <div className="ml-auto w-fit max-w-[85%] rounded-2xl bg-teal-700 px-3 py-2 whitespace-pre-wrap text-white">
                  {entry.text}
                  {entry.heard && <span className="ml-2 text-xs opacity-70">🎙</span>}
                </div>
              ) : entry.kind === "victoria" ? (
                <div className="w-fit max-w-[85%] rounded-2xl bg-black/5 px-3 py-2 whitespace-pre-wrap dark:bg-white/10">
                  {entry.text || "…"}
                </div>
              ) : entry.kind === "notice" ? (
                <div className="rounded-lg border border-current/20 px-3 py-2 text-sm">
                  <strong>{entry.title}</strong>
                  <p className="mt-1 whitespace-pre-wrap">{entry.text}</p>
                </div>
              ) : (
                <NotificationCard item={entry.item} chat={chat} />
              )}
            </li>
          ))}
        </ul>
        <div ref={end} />
      </div>
      <VoiceBar chat={chat} voice={voice} />
      <TextInputComponent
        placeholder="Ask Victoria…"
        onSend={(text) => {
          const trimmed = text.trim();
          if (trimmed) chat.sendMessage(trimmed);
        }}
        buttonLabel="Send"
      />
    </div>
  );
}

/** Voice: SmallWebRTC to the assistant service. Turn detection runs on the server; push-to-talk just gates the mic. */
function VoiceBar({ chat, voice }: { chat: Chat; voice: VoiceHandlers }) {
  const unavailable = voiceUnavailable();
  const transport = usePipecatClientTransportState();
  const { enableMic } = usePipecatClientMicControl();
  const [mode, setMode] = useState<Mode>("hands-free");
  const [state, setState] = useState("");
  const [talking, setTalking] = useState(false);
  const botTurnOpen = useRef(false);
  const connected = transport === "ready";
  const idleState = mode === "hands-free" ? "listening" : "hold to talk";

  useRTVIClientEvent(RTVIEvent.BotReady, () => {
    enableMic(mode === "hands-free");
    setState(idleState);
  });
  useRTVIClientEvent(RTVIEvent.UserStartedSpeaking, () => setState("hearing you…"));
  useRTVIClientEvent(RTVIEvent.UserStoppedSpeaking, () => setState("thinking…"));
  useRTVIClientEvent(RTVIEvent.BotStartedSpeaking, () => setState("speaking"));
  useRTVIClientEvent(RTVIEvent.BotStoppedSpeaking, () => {
    botTurnOpen.current = false;
    setState(idleState);
  });
  useRTVIClientEvent(RTVIEvent.UserTranscript, (data) => {
    if (data.final && data.text.trim()) {
      botTurnOpen.current = false;
      chat.addHeard(data.text.trim());
    }
  });
  useRTVIClientEvent(RTVIEvent.BotOutput, (data) => {
    // Sentence-level text only; word-level updates would repeat it.
    if (data.text_type && data.text_type !== "sentence") return;
    if (data.spoken_status && data.spoken_status !== "new") return;
    chat.addBotSentence(data.text, !botTurnOpen.current);
    botTurnOpen.current = true;
  });
  // Voice sessions send notices and timer updates in the chat protocol's shape.
  useRTVIClientEvent(RTVIEvent.ServerMessage, (data) => {
    if (data && typeof data === "object" && "type" in data) chat.handleServerMessage(data as ServerMessage);
  });
  useRTVIClientEvent(RTVIEvent.Disconnected, () => setState(""));

  useEffect(() => {
    if (connected) {
      enableMic(mode === "hands-free");
      setState(mode === "hands-free" ? "listening" : "hold to talk");
    }
  }, [mode]); // eslint-disable-line react-hooks/exhaustive-deps

  const talk = (active: boolean) => {
    if (!connected || mode !== "push-to-talk") return;
    setTalking(active);
    enableMic(active);
  };

  // The space bar is push-to-talk too, when not typing.
  useEffect(() => {
    const typing = () => document.activeElement instanceof HTMLInputElement || document.activeElement instanceof HTMLTextAreaElement;
    const down = (event: KeyboardEvent) => {
      if (event.code === "Space" && !event.repeat && !typing() && mode === "push-to-talk" && connected) {
        event.preventDefault();
        talk(true);
      }
    };
    const up = (event: KeyboardEvent) => {
      if (event.code === "Space" && !typing()) talk(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  });

  if (unavailable) return <p className="text-sm opacity-70">{unavailable}</p>;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <ConnectButton
        onConnect={() => void voice.connect?.()}
        onDisconnect={() => void voice.disconnect?.()}
        stateContent={{
          disconnected: { children: "Start voice", variant: "primary" },
          ready: { children: "Stop voice", variant: "outline" },
        }}
      />
      {connected && <UserAudioControl size="md" />}
      <div className="flex rounded-lg border border-current/20 text-sm" role="group" aria-label="Voice mode">
        {(["hands-free", "push-to-talk"] as const).map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => setMode(value)}
            className={`px-3 py-1.5 first:rounded-l-lg last:rounded-r-lg ${mode === value ? "bg-teal-700 text-white" : ""}`}
            aria-pressed={mode === value}
          >
            {value === "hands-free" ? "Hands-free" : "Push to talk"}
          </button>
        ))}
      </div>
      {connected && mode === "push-to-talk" && (
        <Button
          variant={talking ? "active" : "outline"}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            talk(true);
          }}
          onPointerUp={() => talk(false)}
          onPointerCancel={() => talk(false)}
        >
          Hold to talk
        </Button>
      )}
      <span className="text-sm opacity-70">{voice.error ? `voice: ${voice.error}` : connected ? state : ""}</span>
    </div>
  );
}
