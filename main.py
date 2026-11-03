import os
import uuid
import time
import math
import chess
import chess.engine
import bcrypt
from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, ForeignKey, Integer, select, desc, or_

DATABASE_URL = "postgresql+asyncpg://root:nnQFWohevFfVx8jGwWqEynZffuxykhOf@dpg-dbl0tebm8hqs738qj3eg-a.oregon-postgres.render.com/hmate"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    elo: Mapped[int] = mapped_column(Integer, default=7500)

class GameModel(Base):
    __tablename__ = "games"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    white_id: Mapped[int] = mapped_column(Integer, nullable=True)
    black_id: Mapped[int] = mapped_column(Integer, nullable=True)
    fen: Mapped[str] = mapped_column(String(255), default=chess.STARTING_FEN)
    pgn_history: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    game_mode: Mapped[str] = mapped_column(String(20), default="pvp")
    player_color: Mapped[str] = mapped_column(String(10), default="white")
    bot_elo: Mapped[int] = mapped_column(Integer, default=1500)
    time_control: Mapped[int] = mapped_column(Integer, default=180)
    white_time: Mapped[float] = mapped_column(default=180.0)
    black_time: Mapped[float] = mapped_column(default=180.0)
    last_move_timestamp: Mapped[float] = mapped_column(default=lambda: time.time())
    elo_delta_white: Mapped[int] = mapped_column(Integer, default=0)
    elo_delta_black: Mapped[int] = mapped_column(Integer, default=0)

app = FastAPI(title="HMate - Liquid Glass Chess")
app.mount("/static", StaticFiles(directory="static"), name="static")

STOCKFISH_PATH = "./stockfish.exe" if os.name == "nt" else "./stockfish"
print("Путь к Stockfish существует:", os.path.exists(STOCKFISH_PATH))
# Глобальная структура ожидания: [{"user_id": int, "time_control": int, "game_id": str | None}]
mm_queue = []

async def get_db():
    async with async_session() as session:
        yield session

@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

class UserAuth(BaseModel):
    username: str
    password: str

class MatchmakingRequest(BaseModel):
    user_id: int
    time_control: int

class MoveRequest(BaseModel):
    game_id: str
    from_sq: str
    to_sq: str
    promotion: str | None = None

class CreateGameRequest(BaseModel):
    user_id: int
    mode: str = "pvp"
    color: str = "white"
    elo: int = 1500

def calculate_cs2_elo(rating_a: int, rating_b: int, score_a: float):
    """
    Линейная формула ELO:
    П = 300 + ((R_соперника - R_наш) / 100)
    score_a: 1.0 (победа), 0.5 (ничья), 0.0 (поражение)
    """
    if score_a == 0.5:
        return 0  # Ничья — очки не меняются (или можно сделать 0)
    
    # Если побеждает игрок A (score_a == 1.0)
    if score_a == 1.0:
        delta = 300 + ((rating_b - rating_a) / 100.0)
        return max(1, round(delta))
    
    # Если проигрывает игрок A (score_a == 0.0) — теряет столько же, сколько выиграл бы соперник
    else:
        # Для проигравшего считаем наоборот: соперник выиграл бы столько:
        delta_opponent = 300 + ((rating_a - rating_b) / 100.0)
        loss = -max(1, round(delta_opponent))
        return loss

def update_game_clocks(game: GameModel):
    if game.status != "active":
        return
    now = time.time()
    elapsed = now - game.last_move_timestamp
    board = chess.Board(game.fen)

    if board.turn == chess.WHITE:
        game.white_time = max(0.0, game.white_time - elapsed)
        if game.white_time <= 0:
            game.status = "black_win"
    else:
        game.black_time = max(0.0, game.black_time - elapsed)
        if game.black_time <= 0:
            game.status = "white_win"

    game.last_move_timestamp = now

async def finalize_elo_if_needed(game: GameModel, db: AsyncSession):
    if game.game_mode == "mm" and game.status in ["white_win", "black_win", "draw"] and game.elo_delta_white == 0 and game.elo_delta_black == 0:
        w_user = await db.get(User, game.white_id) if game.white_id else None
        b_user = await db.get(User, game.black_id) if game.black_id else None

        if w_user and b_user:
            score_w = 1.0 if game.status == "white_win" else (0.5 if game.status == "draw" else 0.0)
            delta_w = calculate_cs2_elo(w_user.elo, b_user.elo, score_w)
            delta_b = -delta_w

            w_user.elo = max(100, w_user.elo + delta_w)
            b_user.elo = max(100, b_user.elo + delta_b)
            game.elo_delta_white = delta_w
            game.elo_delta_black = delta_b

# Страницы
@app.get("/", response_class=HTMLResponse)
def get_auth_page(): return FileResponse("static/index.html")

@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard_page(): return FileResponse("static/dashboard.html")

@app.get("/profile/{user_id}", response_class=HTMLResponse)
def get_profile_page(user_id: int): return FileResponse("static/profile.html")

@app.get("/game/{game_id}", response_class=HTMLResponse)
def get_game_page(game_id: str): return FileResponse("static/game.html")

@app.get("/analysis/{game_id}", response_class=HTMLResponse)
def get_analysis_page(game_id: str): return FileResponse("static/analysis.html")
# Добавь этот блок при старте приложения (в районе @app.on_event("startup"))
@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # Автоматическое создание аккаунта administrator / adm1
    async with async_session() as session:
        res = await session.execute(select(User).where(User.username == "administrator"))
        admin_user = res.scalars().first()
        if not admin_user:
            hashed = bcrypt.hashpw("adm1".encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            admin = User(username="administrator", hashed_password=hashed, elo=99999)
            session.add(admin)
            await session.commit()
class SetEloRequest(BaseModel):
    admin_id: int
    target_user_id: int
    new_elo: int

@app.post("/api/admin/set-elo")
async def admin_set_elo(req: SetEloRequest, db: AsyncSession = Depends(get_db)):
    admin = await db.get(User, req.admin_id)
    if not admin or admin.username != "administrator":
        raise HTTPException(status_code=403, detail="Доступ запрещен. Вы не administrator.")
    
    target = await db.get(User, req.target_user_id)
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    target.elo = req.new_elo
    await db.commit()
    await db.refresh(target)
    return {"status": "success", "new_elo": target.elo}
# Аккаунты
@app.post("/api/auth/register")
async def register(auth: UserAuth, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.username == auth.username))
    if res.scalars().first(): raise HTTPException(400, "Пользователь уже существует")
    hashed = bcrypt.hashpw(auth.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    user = User(username=auth.username, hashed_password=hashed, elo=7500)
    db.add(user); await db.commit(); await db.refresh(user)
    return {"user_id": user.id, "username": user.username, "elo": user.elo}

@app.post("/api/auth/login")
async def login(auth: UserAuth, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.username == auth.username))
    user = res.scalars().first()
    if not user or not bcrypt.checkpw(auth.password.encode('utf-8'), user.hashed_password.encode('utf-8')):
        raise HTTPException(400, "Неверный логин или пароль")
    return {"user_id": user.id, "username": user.username, "elo": user.elo}

@app.get("/api/users/{user_id}")
async def get_user_profile(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user: raise HTTPException(404)
    return {"id": user.id, "username": user.username, "elo": user.elo}

@app.get("/api/users/search/{query}")
async def search_users(query: str, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(User).where(User.username.ilike(f"%{query}%")).limit(10))
    users = res.scalars().all()
    return [{"id": u.id, "username": u.username, "elo": u.elo} for u in users]

# Исправленный Матчмейкинг
@app.post("/api/matchmaking/join")
async def join_matchmaking(req: MatchmakingRequest, db: AsyncSession = Depends(get_db)):
    global mm_queue
    user = await db.get(User, req.user_id)
    if not user: raise HTTPException(404)

    # 1. Проверяем, не нашел ли текущий игрок уже матч (пока ждал в очереди)
    for item in mm_queue:
        if item["user_id"] == req.user_id:
            if item.get("game_id"):
                game_id = item["game_id"]
                # Очищаем запись из очереди после того, как игрок получил свой матч
                mm_queue = [i for i in mm_queue if i["user_id"] != req.user_id]
                return {"status": "matched", "game_id": game_id}
            # Если еще ищет, продолжать ожидание
            return {"status": "searching"}

    # 2. Ищем соперника в очереди с тем же тайм-контролем
    for item in mm_queue:
        if item["user_id"] != req.user_id and item["time_control"] == req.time_control and not item.get("game_id"):
            now = time.time()
            # Первый найденный в очереди становится белыми, текущий зашедший — черными
            white_user_id = item["user_id"]
            black_user_id = req.user_id

            game = GameModel(
                user_id=white_user_id, 
                white_id=white_user_id, 
                black_id=black_user_id,
                game_mode="mm", 
                player_color="white", # Базовый цвет для создателя записи
                time_control=req.time_control,
                white_time=float(req.time_control), 
                black_time=float(req.time_control),
                last_move_timestamp=now, 
                fen=chess.STARTING_FEN, 
                status="active"
            )
            db.add(game)
            await db.commit()
            await db.refresh(game)

            item["game_id"] = game.id
            return {"status": "matched", "game_id": game.id}

    # 3. Если совпадений нет, встаем в очередь первого игрока
    mm_queue.append({"user_id": req.user_id, "time_control": req.time_control, "game_id": None})
    return {"status": "searching"}

@app.post("/api/matchmaking/cancel")
async def cancel_matchmaking(req: MatchmakingRequest):
    global mm_queue
    mm_queue = [i for i in mm_queue if i["user_id"] != req.user_id]
    return {"status": "cancelled"}

@app.post("/api/games/leave/{game_id}")
async def leave_game(game_id: str, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if game and game.status == "active":
        board = chess.Board(game.fen)
        game.status = "black_win" if board.turn == chess.WHITE else "white_win"
        await finalize_elo_if_needed(game, db)
        await db.commit()
    
    global mm_queue
    mm_queue = [i for i in mm_queue if i.get("game_id") != game_id]
    return {"status": "ok"}

# Игры
@app.get("/api/games/list/{user_id}")
async def list_games(user_id: int, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(GameModel).where(or_(GameModel.user_id == user_id, GameModel.white_id == user_id, GameModel.black_id == user_id)).order_by(desc(GameModel.id)))
    games = res.scalars().all()
    return [{"id": g.id, "status": g.status, "fen": g.fen, "mode": g.game_mode} for g in games]

@app.post("/api/games/new")
async def create_game(req: CreateGameRequest, db: AsyncSession = Depends(get_db)):
    board = chess.Board()
    now = time.time()
    game = GameModel(
        user_id=req.user_id, 
        white_id=req.user_id if req.color == 'white' else None,
        black_id=req.user_id if req.color == 'black' else None,
        fen=board.fen(), 
        pgn_history="", 
        status="active",
        game_mode=req.mode, 
        player_color=req.color, 
        bot_elo=req.elo,
        last_move_timestamp=now
    )
    db.add(game); await db.commit(); await db.refresh(game)
    return {"game_id": game.id}
@app.get("/api/games/{game_id}")
async def get_game(game_id: str, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if not game: raise HTTPException(404)
    update_game_clocks(game)
    await finalize_elo_if_needed(game, db)
    await db.commit()

    board = chess.Board(game.fen)
    w_user = await db.get(User, game.white_id) if game.white_id else None
    b_user = await db.get(User, game.black_id) if game.black_id else None

    return {
        "fen": game.fen, "turn": "white" if board.turn == chess.WHITE else "black",
        "is_check": board.is_check(), "is_game_over": board.is_game_over(), "status": game.status,
        # Точное определение цвета для текущей сессии матчмейкинга
        "player_color": game.player_color if game.game_mode != "mm" else "white", 
        "game_mode": game.game_mode,
        "white_id": game.white_id, "black_id": game.black_id,
        "white_name": w_user.username if w_user else "Белые",
        "black_name": b_user.username if b_user else ("Стокфиш" if game.game_mode == "bot" else "Черные"),
        "white_time": round(game.white_time, 1), "black_time": round(game.black_time, 1),
        "legal_moves": [m.uci() for m in board.legal_moves],
        "history": game.pgn_history.split(",") if game.pgn_history else [],
        "elo_delta_white": game.elo_delta_white, "elo_delta_black": game.elo_delta_black
    }
@app.get("/api/games/{game_id}/hint")
async def get_hint(game_id: str, user_id: int | None = None, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if not game or not os.path.exists(STOCKFISH_PATH): 
        raise HTTPException(400, "Движок не найден")
    
    # Проверяем, является ли пользователь администратором
    is_admin = False
    if user_id:
        user = await db.get(User, user_id)
        if user and user.username == "administrator":
            is_admin = True

    # Для обычных пользователей подсказки в рейтинговом матчмейкинге (MM) отключены
    if game.game_mode == "mm" and not is_admin:
        raise HTTPException(400, "Подсказки выключены в рейтинговом режиме")

    board = chess.Board(game.fen)
    with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
        result = engine.play(board, chess.engine.Limit(time=0.2))
        return {"best_move": result.move.uci()}

@app.post("/api/games/move")
async def make_move(req: MoveRequest, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, req.game_id)
    if not game or game.status != "active": raise HTTPException(400)
    
    update_game_clocks(game)
    if game.status != "active":
        await db.commit()
        return {"status": game.status}

    board = chess.Board(game.fen)
    uci_str = f"{req.from_sq}{req.to_sq}"
    if req.promotion: uci_str += req.promotion.lower()

    try:
        move = chess.Move.from_uci(uci_str)
        if move not in board.legal_moves: raise HTTPException(400)

        san_move = board.san(move)
        board.push(move)
        moves = game.pgn_history.split(",") if game.pgn_history else []
        moves.append(san_move)

        if board.is_game_over():
            if board.is_checkmate():
                game.status = "black_win" if board.turn == chess.WHITE else "white_win"
            else:
                game.status = "draw"
            await finalize_elo_if_needed(game, db)

        game.fen = board.fen()
        game.pgn_history = ",".join([m for m in moves if m])
        game.last_move_timestamp = time.time()
        await db.commit()

        return {
            "fen": game.fen, "turn": "white" if board.turn == chess.WHITE else "black",
            "is_check": board.is_check(), "is_game_over": board.is_game_over(), "status": game.status,
            "white_time": round(game.white_time, 1), "black_time": round(game.black_time, 1),
            "legal_moves": [m.uci() for m in board.legal_moves],
            "elo_delta_white": game.elo_delta_white, "elo_delta_black": game.elo_delta_black
        }
    except Exception as e: raise HTTPException(400, str(e))

@app.post("/api/games/{game_id}/bot-move")
async def bot_move(game_id: str, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if not game or game.status != "active" or game.game_mode != "bot": raise HTTPException(400)
    
    board = chess.Board(game.fen)
    if board.is_game_over(): return {"status": game.status}

    if os.path.exists(STOCKFISH_PATH):
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            if game.bot_elo < 3000: engine.configure({"UCI_LimitStrength": True, "UCI_Elo": game.bot_elo})
            bot_result = engine.play(board, chess.engine.Limit(time=0.2))
            bot_san = board.san(bot_result.move)
            board.push(bot_result.move)

            moves = game.pgn_history.split(",") if game.pgn_history else []
            moves.append(bot_san)
            game.fen = board.fen()
            game.pgn_history = ",".join([m for m in moves if m])
            game.last_move_timestamp = time.time()

            if board.is_game_over():
                if board.is_checkmate():
                    game.status = "black_win" if board.turn == chess.WHITE else "white_win"
                else: game.status = "draw"

            await db.commit()
            return {
                "fen": game.fen, "turn": "white" if board.turn == chess.WHITE else "black",
                "is_check": board.is_check(), "is_game_over": board.is_game_over(), "status": game.status,
                "legal_moves": [m.uci() for m in board.legal_moves]
            }
    raise HTTPException(500)

@app.post("/api/games/{game_id}/resign")
async def resign_game(game_id: str, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if not game or game.status != "active": raise HTTPException(404)
    board = chess.Board(game.fen)
    game.status = "black_win" if board.turn == chess.WHITE else "white_win"
    await finalize_elo_if_needed(game, db)
    await db.commit()
    return {
        "status": game.status,
        "elo_delta_white": game.elo_delta_white,
        "elo_delta_black": game.elo_delta_black
    }
def analyze_move(board_before: chess.Board, move: chess.Move):
    if not os.path.exists(STOCKFISH_PATH):
        return "good"
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            limit = chess.engine.Limit(time=0.03)
            score_before = engine.analyse(board_before, limit)["score"].relative.score(mate_score=10000)
            tb = board_before.copy()
            tb.push(move)
            score_after = -engine.analyse(tb, limit)["score"].relative.score(mate_score=10000)
            diff = (score_after or 0) - (score_before or 0)
            
            best_result = engine.play(board_before, chess.engine.Limit(time=0.02))
            best = best_result.move == move if best_result.move else False
            
            if best and diff >= 20: return "brilliant"
            elif best: return "best"
            elif diff >= -15: return "excellent"
            elif diff >= -50: return "good"
            elif diff >= -150: return "inaccuracy"
            elif diff >= -300: return "mistake"
            else: return "blunder"
    except Exception as e:
        print(f"Ошибка Stockfish: {e}")
        return "good"
MOVE_ICONS = {
    "brilliant": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/php4ena5mpf1bjlf8hccpv.png",
    "great": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpobj5vs9pecrl4BZmIPd.png",
    "best": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpvjtpi9m3qbbgfvXvhZR.png",
    "excellent": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpr0g4j3fddqdabQwH119.png",
    "good": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phprpbn5fvlm4nv94nJhKZ.png",
    "book": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpkr7oe5aum6gqbz2W8KY.png",
    "inaccuracy": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpuaotu0b8ovcr9payx0o.png",
    "mistake": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpi81juimlh0mtfh6D5vQ.png",
    "blunder": "https://images.chesscomfiles.com/uploads/v1/images_users/tiny_mce/Wind/phpj69kvm6s0nu2ftFs7LH.png"
}
@app.get("/api/games/{game_id}/full-analysis")
async def full_analysis(game_id: str, db: AsyncSession = Depends(get_db)):
    game = await db.get(GameModel, game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Игра не найдена")
        
    moves = game.pgn_history.split(",") if game.pgn_history else []
    board = chess.Board()
    analyzed_steps = []
    
    for san in moves:
        if not san: 
            continue
        try:
            move = board.parse_san(san)
            fen_before = board.fen()
            rating = analyze_move(board, move)
            board.push(move)
            analyzed_steps.append({
                "san": san,
                "fen_before": fen_before,
                "fen_after": board.fen(),
                "rating": rating,
                "icon": MOVE_ICONS.get(rating, MOVE_ICONS["good"])
            })
        except Exception:
            continue
            
    return {"steps": analyzed_steps, "status": game.status}
