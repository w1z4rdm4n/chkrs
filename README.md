# Checkers — Two-Player WebSocket Edition

A real-time two-player checkers game. Game state lives on the server; both
browsers connect via WebSocket and see the same board instantly.

## Files

```
checkers/
├── server.py        ← Python WebSocket server (owns all game state)
├── index.html       ← Single-page client (HTML + CSS + JS)
├── requirements.txt ← websockets==13.1
├── Dockerfile
└── README.md
```

## Build & Run

```bash
# 1. Build the image
docker build -t checkers .

# 2. Run the container
docker run -d -p 8080:8080 --name checkers checkers

# 3. Open two browser tabs (or two different browsers / devices on the same network)
open http://localhost:8080   # first tab  → assigned Red
open http://localhost:8080   # second tab → assigned Black

# Any further visitors join as spectators (read-only view).
```

## Stop / Remove

```bash
docker stop checkers && docker rm checkers
```

## How it works

- **First visitor** is assigned **Red**, shown a "Waiting for opponent…" overlay.
- **Second visitor** is assigned **Black**; both overlays dismiss and the game begins.
- Every move is sent as a JSON WebSocket message to the server, which validates it
  against the authoritative game state and broadcasts the new state to all clients.
- If a player disconnects, their opponent sees a notice; the slot is freed and the
  next visitor who connects claims it.
- Either player can click **New Game** at any time to reset the board for everyone.
- **Black's board is flipped** so both players see their own pieces at the bottom.

## Rules (American Checkers)

- Red moves first, toward the top of the board.
- Pieces move diagonally on dark squares only.
- **Mandatory jumps** — if a capture is available you must take it.
- **Multi-jumps** — a piece that can keep jumping must continue.
- A piece reaching the far end becomes a **King ♛** and can move backward.
- Win by capturing all opponent pieces or leaving them with no legal moves.
