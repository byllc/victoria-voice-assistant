import { useCallback, useEffect, useState } from "react";

import {
  Badge,
  Banner,
  BannerAction,
  BannerTitle,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  PipecatAppBase,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  ThemeModeToggle,
} from "@pipecat-ai/voice-ui-kit";

import { AlertsView } from "./AlertsView";
import { ChatView } from "./ChatView";
import { TasksView } from "./TasksView";
import { Timers } from "./Timers";
import { useChat } from "./useChat";
import { ACTIVE_STATES } from "./types";

type Session = { authenticated: boolean; insecure: boolean };

export function App() {
  const [session, setSession] = useState<Session | null>(null);

  const check = useCallback(async () => {
    const response = await fetch("/api/session", { credentials: "same-origin" });
    const body = (await response.json()) as { authenticated: boolean; insecure?: boolean };
    setSession({ authenticated: body.authenticated, insecure: Boolean(body.insecure) });
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  return (
    <PipecatAppBase
      transportType="smallwebrtc"
      // Signaling goes through the web interface; audio flows directly to the assistant service.
      connectParams={{ webrtcRequestParams: { endpoint: "/api/offer" } }}
      clientOptions={{ enableMic: true, enableCam: false }}
      initDevicesOnMount={false}
      themeProps={{ defaultTheme: "system" }}
    >
      {/* The kit renders once before its client exists, outside the client context; wait for the client. */}
      {({ client, handleConnect, handleDisconnect, error }) =>
        session === null || client === null ? null : session.authenticated ? (
          <Main
            insecure={session.insecure}
            onSignedOut={() => setSession({ authenticated: false, insecure: false })}
            voice={{ connect: handleConnect, disconnect: handleDisconnect, error: error ?? null }}
          />
        ) : (
          <Login onSignedIn={check} />
        )
      }
    </PipecatAppBase>
  );
}

export type VoiceHandlers = {
  connect?: () => void | Promise<void>;
  disconnect?: () => void | Promise<void>;
  error: string | null;
};

function Main({ insecure, onSignedOut, voice }: { insecure: boolean; onSignedOut: () => void; voice: VoiceHandlers }) {
  const chat = useChat(onSignedOut);
  const [tab, setTab] = useState("chat");
  const active = chat.dashboard?.tasks.filter((t) => ACTIVE_STATES.includes(t.state)).length ?? 0;
  const waiting = chat.dashboard?.summary.waiting ?? 0;
  const unread = chat.notifications.length;

  const signOut = async () => {
    await voice.disconnect?.();
    await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
    chat.clearLog();
    onSignedOut();
  };

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col gap-2 p-3 sm:p-4">
      {insecure && (
        <Banner variant="destructive" inset>
          <BannerTitle>Open to the network: no household password is set (ASSISTANT_WEB_PASSWORD).</BannerTitle>
        </Banner>
      )}
      <header className="flex items-center gap-2">
        <h1 className="flex-1 text-lg font-semibold">Victoria</h1>
        <Badge color={chat.status === "connected" ? "active" : chat.status === "thinking" ? "warning" : "inactive"} variant="outline">
          {chat.status}
        </Badge>
        <ThemeModeToggle />
        {!insecure && (
          <Button size="sm" variant="ghost" onClick={() => void signOut()}>
            Sign out
          </Button>
        )}
      </header>
      {chat.ringing && (
        <Banner variant="destructive" visible>
          <BannerTitle>{chat.ringing.text}</BannerTitle>
          <BannerAction
            onClick={() => {
              chat.send({ type: "timer_stop" });
              chat.stopRinging();
            }}
          >
            Stop
          </BannerAction>
        </Banner>
      )}
      <Timers timers={chat.timers} />
      <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
        <TabsList className="w-full">
          <TabsTrigger value="chat" className="flex-1">
            Chat
          </TabsTrigger>
          <TabsTrigger value="tasks" className="flex-1 gap-2">
            Tasks {active > 0 && <Badge color={waiting ? "warning" : "primary"} size="sm">{active}</Badge>}
          </TabsTrigger>
          <TabsTrigger value="alerts" className="flex-1 gap-2">
            Alerts {unread > 0 && <Badge color="warning" size="sm">{unread}</Badge>}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="chat" className="flex min-h-0 flex-1 flex-col">
          <ChatView chat={chat} voice={voice} />
        </TabsContent>
        <TabsContent value="tasks" className="min-h-0 flex-1 overflow-y-auto">
          <TasksView chat={chat} />
        </TabsContent>
        <TabsContent value="alerts" className="min-h-0 flex-1 overflow-y-auto">
          <AlertsView chat={chat} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
      credentials: "same-origin",
    });
    if (response.ok) onSignedIn();
    else {
      setPassword("");
      setError("Incorrect password.");
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Victoria</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3">
            <label htmlFor="password" className="text-sm">
              Household password
            </label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
              autoFocus
            />
            <Button type="submit">Sign in</Button>
            {error && (
              <p className="text-sm text-red-600" role="alert">
                {error}
              </p>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
