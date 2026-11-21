# Saved chats: projects and contexts

Typed chats in the web interface are saved. Each chat is a **context**, and chats are grouped into **projects**. Architecture §4.1 has the design.

## Using it

- **Refreshing keeps the chat.** Reloading the page, or opening the dashboard on another device, brings back the chat you had open, and Victoria remembers what was said in it.
- **New chat** (next to the chat's name) starts an empty chat. Victoria doesn't know what was said in other chats. It's saved, and named from your first message, once you send something.
- **Your chats:** click the chat's name (`General / …`) to see every project and its chats. Click one to open it.
  - **+ Chat** starts a new chat in that project.
  - **Rename** works on chats and projects.
  - **Archive** hides a chat, or a project with all its chats. Nothing is deleted.
  - **New project** makes a project and opens a new chat in it.
  - **Archived** lists what's archived. Click a chat to restore and open it, or **Restore** a project.
- You can't switch chats while Victoria is still answering.

Everyone who signs in with the household password sees the same projects and chats.

## What Victoria remembers

When a chat is opened, its last 20 messages go back to the model, without old tool results. Exchanges that used tools whose answers go out of date (such as the clock, timers and weather: any tool with `forget_after_s` in `tools.yaml`) are left out, so an old time or weather answer isn't repeated. The screen shows the whole chat.

Voice isn't part of this yet. Satellites and browser voice keep their own short conversation, as before.

## Where it's kept

The assistant's database (`~/Library/Application Support/Victoria Voice Assistant/assistant.sqlite3`): tables `projects`, `contexts`, `context_messages`, and `client_contexts` (which chat each browser had open). The nightly backup carries it. Each command-history entry also records its chat (`history.context_id`).
