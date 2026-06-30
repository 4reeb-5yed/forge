# Frontend

The frontend is a responsive Next.js 14 application that provides a real-time interface to Forge builds. It lives in `frontend/`.

## Tech Stack

| Technology | Version | Purpose |
|-----------|---------|---------|
| Next.js | 14.2+ | React framework with App Router |
| React | 18.3+ | UI library |
| TypeScript | 5.4+ | Type safety |
| Tailwind CSS | 3.4+ | Utility-first styling |
| PostCSS | 8.4+ | CSS processing |

## Project Structure

```
frontend/
├── app/
│   ├── globals.css          # Tailwind directives + custom styles
│   ├── layout.tsx           # Root layout (html, body, fonts)
│   └── page.tsx             # Main page (chat + sidebar + events)
├── components/
│   ├── ChatInput.tsx        # Message input with send button
│   ├── ChatMessage.tsx      # Individual message rendering
│   ├── EventLog.tsx         # Real-time event stream panel
│   ├── SessionList.tsx      # Sidebar with session management
│   └── StatusBar.tsx        # Runtime status + control buttons
├── lib/
│   └── api.ts              # API client (REST + WebSocket)
├── next.config.ts          # Next.js configuration (API rewrites)
├── tailwind.config.ts      # Tailwind theme (forge-* colors)
├── tsconfig.json           # TypeScript configuration
└── package.json
```

## Component Architecture

```mermaid
graph TD
    Page[page.tsx - Main Page]
    Page --> SL[SessionList]
    Page --> SB[StatusBar]
    Page --> CM[ChatMessage]
    Page --> CI[ChatInput]
    Page --> EL[EventLog]

    SL -->|onSelectSession| Page
    CI -->|onSend| Page
    SB -->|onInterrupt/Resume/Stop| Page
    
    Page -->|REST| API[Backend API]
    Page -->|WebSocket| WS[Event Stream]
```

### `page.tsx` — Main Page

The root page component manages all state:
- **Session state:** Active session, sidebar visibility
- **Chat state:** Message history, invoke status
- **Event state:** WebSocket connection, event log
- **Runtime state:** Polled status from backend

Layout: Three-panel responsive design:
- Left: Session sidebar (collapsible on mobile)
- Center: Chat messages + input
- Right: Event log (collapsible)

### `ChatInput.tsx`

Text input with send button. Disabled when no session is active or a build is in progress.

### `ChatMessage.tsx`

Renders user messages, system responses, and loading states. Exports the `Message` type:

```typescript
interface Message {
  id: string;
  role: "user" | "system";
  content: string;
  timestamp: Date;
  isLoading?: boolean;
  response?: InvokeResponse;
}
```

### `SessionList.tsx`

Sidebar showing all sessions with create/select/delete actions. Includes responsive mobile overlay.

### `EventLog.tsx`

Real-time scrolling log of `SessionEvent` objects received via WebSocket. Shows event type, source, and timestamp.

### `StatusBar.tsx`

Displays runtime status (current node, active task, budget) and control buttons (Interrupt, Resume, Stop).

## API Client

**File:** `frontend/lib/api.ts`

All backend communication goes through a single API client module.

### REST Endpoints

```typescript
// Sessions
listSessions(): Promise<Session[]>
createSession(payload: CreateSessionPayload): Promise<Session>
getSession(id: string): Promise<Session>
deleteSession(id: string): Promise<void>

// Workflow
invokeWorkflow(payload: InvokePayload): Promise<InvokeResponse>

// Inspection
getSessionStatus(id: string): Promise<RuntimeStatus>
getExplanation(id: string): Promise<DecisionExplanation>

// Control
interruptSession(id: string): Promise<{status: string}>
resumeSession(id: string): Promise<{status: string}>
stopSession(id: string): Promise<{status: string}>
```

### WebSocket Connection

```typescript
function connectEventStream(
  sessionId: string,
  onEvent: (event: SessionEvent) => void,
  onError?: (error: Event) => void,
  onClose?: () => void
): WebSocket
```

Connects to `ws://host/api/sessions/{id}/events` and parses incoming JSON messages as `SessionEvent` objects.

### Request Routing

All API calls go to `/api/*` which Next.js rewrites to `localhost:8000/*` (the backend). This avoids CORS issues in development.

## Responsive Design

The UI is fully responsive with three breakpoints:

| Breakpoint | Layout |
|-----------|--------|
| Mobile (<768px) | Sidebar as overlay, event log collapsed below chat |
| Tablet (768px+) | Fixed sidebar, event log toggle |
| Desktop (1024px+) | All three panels visible |

Key patterns:
- **Mobile sidebar:** Full-screen overlay with backdrop, toggled by hamburger menu
- **Event log:** Collapsible panel (bottom on mobile, right side on desktop)
- **Chat:** Always centered and visible, fills available space

## Custom Theme

Tailwind is extended with Forge-specific design tokens:

```css
/* Custom color tokens used throughout */
--forge-bg: ...       /* Background */
--forge-card: ...     /* Card backgrounds */
--forge-border: ...   /* Borders */
--forge-text: ...     /* Primary text */
--forge-muted: ...    /* Secondary text */
```

## How to Run

### Development

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

The dev server hot-reloads on file changes.

### Production Build

```bash
cd frontend
npm run build
npm start
```

### Linting

```bash
npm run lint
```

## How to Develop

### Adding a New Component

1. Create `frontend/components/YourComponent.tsx`
2. Use the `"use client"` directive if it uses hooks or browser APIs
3. Import and use in `page.tsx` or another component
4. Follow existing patterns: TypeScript props interface, Tailwind classes, responsive design

### Adding a New API Endpoint

1. Add types to `frontend/lib/api.ts`
2. Add the request function
3. Use it from a component with appropriate loading/error states

### State Management

The app uses React's built-in `useState` and `useEffect` — no external state library. State lives in `page.tsx` and is passed down as props. For a larger app, consider extracting to a context or state library.
