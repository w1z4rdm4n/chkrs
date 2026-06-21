"""
Checkers WebSocket server.

Protocol (JSON messages):
  Server → Client:
    {type:"welcome",   color:"red"|"blk", waiting:bool}
    {type:"state",     state:{board, turn, mustJump, chainPiece, captured, gameOver, winner}}
    {type:"rejected",  reason:str}
    {type:"opponent_disconnected"}
    {type:"spectator"}   – third+ connections are spectators (read-only)

  Client → Server:
    {type:"move",  sr,sc,tr,tc}
    {type:"new_game"}
"""

import asyncio, json, logging
from http import HTTPStatus
from pathlib import Path
import websockets
from websockets.server import serve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

# ── Checkers logic (pure Python, mirrors client JS) ─────────────────────────

RED, BLK = "red", "blk"

def fresh_board():
    board = [[None]*8 for _ in range(8)]
    for r in range(3):
        for c in range(8):
            if (r+c) % 2 == 1:
                board[r][c] = {"color": BLK, "king": False}
    for r in range(5, 8):
        for c in range(8):
            if (r+c) % 2 == 1:
                board[r][c] = {"color": RED, "king": False}
    return board

def get_jumps(r, c, pc, board):
    if pc["king"]:
        dirs = [(-1,-1),(-1,1),(1,-1),(1,1)]
    elif pc["color"] == RED:
        dirs = [(-1,-1),(-1,1)]
    else:
        dirs = [(1,-1),(1,1)]
    jumps = []
    for dr, dc in dirs:
        mr, mc = r+dr, c+dc
        lr, lc = r+2*dr, c+2*dc
        if not (0 <= lr <= 7 and 0 <= lc <= 7):
            continue
        mid = board[mr][mc]
        if mid and mid["color"] != pc["color"] and board[lr][lc] is None:
            jumps.append({"r": lr, "c": lc, "captures": [{"r": mr, "c": mc}]})
    return jumps

def get_simple_moves(r, c, pc, board):
    if pc["king"]:
        dirs = [(-1,-1),(-1,1),(1,-1),(1,1)]
    elif pc["color"] == RED:
        dirs = [(-1,-1),(-1,1)]
    else:
        dirs = [(1,-1),(1,1)]
    moves = []
    for dr, dc in dirs:
        nr, nc = r+dr, c+dc
        if 0 <= nr <= 7 and 0 <= nc <= 7 and board[nr][nc] is None:
            moves.append({"r": nr, "c": nc, "captures": []})
    return moves

def all_jumps_for_color(color, board):
    jumps = []
    for r in range(8):
        for c in range(8):
            pc = board[r][c]
            if pc and pc["color"] == color:
                for j in get_jumps(r, c, pc, board):
                    jumps.append({"sr": r, "sc": c, **j})
    return jumps

def moves_for_piece(r, c, turn, must_jump, board):
    pc = board[r][c]
    if not pc or pc["color"] != turn:
        return []
    jumps = get_jumps(r, c, pc, board)
    if must_jump:
        return jumps
    if all_jumps_for_color(turn, board):
        return jumps
    return get_simple_moves(r, c, pc, board)

def check_winner(board, turn):
    red_count = sum(1 for r in board for p in r if p and p["color"] == RED)
    blk_count = sum(1 for r in board for p in r if p and p["color"] == BLK)
    if red_count == 0:
        return BLK
    if blk_count == 0:
        return RED
    # No legal moves for current player?
    has_move = any(
        moves_for_piece(r, c, turn, bool(all_jumps_for_color(turn, board)), board)
        for r in range(8) for c in range(8)
        if board[r][c] and board[r][c]["color"] == turn
    )
    if not has_move:
        return BLK if turn == RED else RED
    return None

# ── Game room ────────────────────────────────────────────────────────────────

class Game:
    def __init__(self):
        self.reset()
        self.players = {}      # color -> websocket
        self.spectators = set()

    def reset(self):
        self.board      = fresh_board()
        self.turn       = RED
        self.must_jump  = False
        self.chain_piece= None   # {"r","c"} or None
        self.captured   = {RED: 0, BLK: 0}
        self.game_over  = False
        self.winner     = None
        self._recompute_must_jump()

    def _recompute_must_jump(self):
        self.must_jump = bool(all_jumps_for_color(self.turn, self.board))

    def public_state(self):
        return {
            "board":       self.board,
            "turn":        self.turn,
            "mustJump":    self.must_jump,
            "chainPiece":  self.chain_piece,
            "captured":    self.captured,
            "gameOver":    self.game_over,
            "winner":      self.winner,
        }

    def apply_move(self, color, sr, sc, tr, tc):
        """Validate and apply a move. Returns (ok, error_reason)."""
        if self.game_over:
            return False, "Game is over"
        if color != self.turn:
            return False, "Not your turn"
        if self.chain_piece and (sr != self.chain_piece["r"] or sc != self.chain_piece["c"]):
            return False, "Must continue jumping with the same piece"

        legal = moves_for_piece(sr, sc, self.turn, self.must_jump, self.board)
        match = next((m for m in legal if m["r"] == tr and m["c"] == tc), None)
        if match is None:
            return False, "Illegal move"

        pc = self.board[sr][sc]
        caps = match["captures"]

        # Execute
        self.board[tr][tc] = pc
        self.board[sr][sc] = None
        for cap in caps:
            self.board[cap["r"]][cap["c"]] = None
        self.captured[color] += len(caps)

        # King promotion
        if not pc["king"]:
            if pc["color"] == RED and tr == 0:
                pc["king"] = True
            elif pc["color"] == BLK and tr == 7:
                pc["king"] = True

        # Chain jump?
        if caps:
            further = get_jumps(tr, tc, pc, self.board)
            if further:
                self.chain_piece = {"r": tr, "c": tc}
                self.must_jump = True
                return True, None   # same player's turn continues

        # End of turn
        self.chain_piece = None
        self.turn = BLK if self.turn == RED else RED
        self._recompute_must_jump()

        w = check_winner(self.board, self.turn)
        if w:
            self.game_over = True
            self.winner = w

        return True, None

# ── Single shared game room ──────────────────────────────────────────────────

game = Game()

async def broadcast_state():
    msg = json.dumps({"type": "state", "state": game.public_state()})
    targets = list(game.players.values()) + list(game.spectators)
    if targets:
        await asyncio.gather(*[ws.send(msg) for ws in targets], return_exceptions=True)

# ── HTTP handler for serving index.html ─────────────────────────────────────

INDEX_HTML = Path(__file__).parent / "index.html"

async def http_handler(path, request_headers):
    """Serve index.html for any HTTP GET request (websockets library hook)."""
    if request_headers.get("Upgrade", "").lower() == "websocket":
        return None   # let websocket handler take over
    body = INDEX_HTML.read_bytes()
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ]
    return HTTPStatus.OK, headers, body

# ── WebSocket handler ────────────────────────────────────────────────────────

async def handler(ws):
    global game

    # Assign role
    if RED not in game.players:
        color = RED
        game.players[RED] = ws
        log.info("Red player connected")
    elif BLK not in game.players:
        color = BLK
        game.players[BLK] = ws
        log.info("Black player connected")
    else:
        color = None   # spectator
        game.spectators.add(ws)
        log.info("Spectator connected")

    try:
        if color is None:
            await ws.send(json.dumps({"type": "spectator"}))
            await ws.send(json.dumps({"type": "state", "state": game.public_state()}))
            # Spectators just receive; they can't send moves
            async for _ in ws:
                pass
            return

        waiting = len(game.players) < 2
        await ws.send(json.dumps({"type": "welcome", "color": color, "waiting": waiting}))
        await ws.send(json.dumps({"type": "state", "state": game.public_state()}))

        # If second player just joined, tell first player to stop waiting
        if not waiting:
            other_color = BLK if color == RED else RED
            other_ws = game.players.get(other_color)
            if other_ws:
                try:
                    await other_ws.send(json.dumps({"type": "welcome", "color": other_color, "waiting": False}))
                except Exception:
                    pass

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "new_game":
                if color in (RED, BLK):
                    game.reset()
                    log.info(f"{color} requested new game")
                    await broadcast_state()

            elif msg.get("type") == "move":
                sr, sc = int(msg["sr"]), int(msg["sc"])
                tr, tc = int(msg["tr"]), int(msg["tc"])
                ok, reason = game.apply_move(color, sr, sc, tr, tc)
                if ok:
                    log.info(f"{color} moved ({sr},{sc})→({tr},{tc})")
                    await broadcast_state()
                else:
                    await ws.send(json.dumps({"type": "rejected", "reason": reason}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if color and game.players.get(color) is ws:
            del game.players[color]
            log.info(f"{color} disconnected")
            # Notify remaining player
            other = game.players.get(BLK if color == RED else RED)
            if other:
                try:
                    await other.send(json.dumps({"type": "opponent_disconnected"}))
                except Exception:
                    pass
        elif ws in game.spectators:
            game.spectators.discard(ws)

async def main():
    log.info("Starting checkers server on ws://0.0.0.0:8080")
    async with serve(handler, "0.0.0.0", 8080, process_request=http_handler):
        await asyncio.Future()   # run forever

if __name__ == "__main__":
    asyncio.run(main())
