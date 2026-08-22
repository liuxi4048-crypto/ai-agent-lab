ファイル: index.html
```html
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>ブロック崩し</title>
<style>
  body { margin:0; background:#111; color:#fff; font-family:Arial,Helvetica,sans-serif; overflow:hidden; }
  canvas { display:block; margin:auto; background:#222; cursor:default; }
</style>
</head>
<body>
<canvas id="canvas" width="800" height="600"></canvas>
<script>
(() => {
  const canvas = document.getElementById('canvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;

  // Game state
  let score = 0;
  let gameOver = false, gameClear = false, paused = true;

  // Paddle
  const paddle = {
    w: 100, h: 15,
    x: W/2 - 50, y: H - 30,
    dx: 0, speed: 6
  };

  // Ball
  const ball = {
    r: 10,
    x: W/2, y: H/2,
    vx: 4, vy: -4,
    alive: true
  };

  // Blocks
  const blocks = [];
  const blockW = 60, blockH = 20, blockPadding = 10;
  const rowCount = 5, colCount = 11;
  const offsetTop = 60, offsetLeft = 35;

  function createBlocks() {
    blocks.length = 0;
    for(let c=0; c<colCount; c++){
      for(let r=0; r<rowCount; r++){
        const b = {
          x: offsetLeft + c*(blockW+blockPadding),
          y: offsetTop + r*(blockH+blockPadding),
          w: blockW, h: blockH,
          status: 1
        };
        blocks.push(b);
      }
    }
  }

  // Input
  canvas.addEventListener('mousemove', e => {
    const rect = canvas.getBoundingClientRect();
    const posX = e.clientX - rect.left;
    paddle.x = Math.max(0, Math.min(W - paddle.w, posX - paddle.w/2));
  });

  let keyLeft = false, keyRight = false;
  document.addEventListener('keydown', e => {
    if(e.code === 'ArrowLeft') keyLeft = true;
    if(e.code === 'ArrowRight') keyRight = true;
    if(e.code === 'Space') togglePause();
  });
  document.addEventListener('keyup', e => {
    if(e.code === 'ArrowLeft') keyLeft = false;
    if(e.code === 'ArrowRight') keyRight = false;
  });

  canvas.addEventListener('mousedown', () => {
    if(gameOver || gameClear) startGame();
  });

  function togglePause() {
    if (!gameOver && !gameClear) {
      paused = !paused;
    }
  }

  function resetGame() {
    score = 0;
    gameOver = false;
    gameClear = false;
    paused = true;
    paddle.x = W/2 - paddle.w/2;
    ball.x = W/2; ball.y = H/2;
    ball.vx = 4; ball.vy = -4;
    createBlocks();
  }

  function startGame() {
    if (gameOver || gameClear) resetGame();
    paused = false;
  }

  function drawPaddle() {
    ctx.fillStyle = '#0095DD';
    ctx.fillRect(paddle.x, paddle.y, paddle.w, paddle.h);
  }

  function drawBall() {
    ctx.beginPath();
    ctx.arc(ball.x, ball.y, ball.r, 0, Math.PI*2);
    ctx.fillStyle = '#0095DD';
    ctx.fill();
    ctx.closePath();
  }

  function drawBlocks() {
    blocks.forEach(b => {
      if(b.status){
        ctx.fillStyle = '#FF4500';
        ctx.fillRect(b.x, b.y, b.w, b.h);
      }
    });
  }

  function drawScore() {
    ctx.font = '20px Arial';
    ctx.fillStyle = '#fff';
    ctx.fillText('Score: ' + score, 20, 30);
  }

  function drawMessage(msg, sub) {
    ctx.fillStyle = 'rgba(0,0,0,0.7)';
    ctx.fillRect(0,0,W,H);
    ctx.font = '48px Arial';
    ctx.fillStyle = '#fff';
    ctx.textAlign='center';
    ctx.fillText(msg, W/2, H/2 - 30);
    ctx.font = '24px Arial';
    ctx.fillText(sub, W/2, H/2 + 20);
  }

  function updateBlocks() {
    // collision detection
    blocks.forEach(b => {
      if(b.status){
        if(ball.x + ball.r > b.x && ball.x - ball.r < b.x + b.w &&
           ball.y + ball.r > b.y && ball.y - ball.r < b.y + b.h){
          ball.vy = -ball.vy;
          b.status = 0;
          score += 10;
        }
      }
    });
    // check win
    if(blocks.every(b=>!b.status)){
      gameClear = true;
      paused = true;
    }
  }

  function updateBall() {
    ball.x += ball.vx;
    ball.y += ball.vy;

    // left/right walls
    if(ball.x + ball.r > W || ball.x - ball.r < 0){
      ball.vx = -ball.vx;
    }
    // top wall
    if(ball.y - ball.r < 0){
      ball.vy = -ball.vy;
    }
    // bottom wall -> game over
    if(ball.y + ball.r > H){
      gameOver = true;
      paused = true;
    }
    // paddle collision
    if(ball.y + ball.r > paddle.y &&
       ball.x > paddle.x && ball.x < paddle.x + paddle.w){
      ball.vy = -ball.vy;
      // adjust vx based on hit position
      let hitPos = (ball.x - paddle.x) / paddle.w - 0.5;
      ball.vx += hitPos * 2;
    }
  }

  function updatePaddle() {
    if(keyLeft) paddle.x -= paddle.speed;
    if(keyRight) paddle.x += paddle.speed;
    paddle.x = Math.max(0, Math.min(W - paddle.w, paddle.x));
  }

  function loop() {
    ctx.clearRect(0,0,W,H);
    drawBlocks();
    drawPaddle();
    drawBall();
    drawScore();

    if(!paused){
      updatePaddle();
      updateBall();
      updateBlocks();
    }

    if(gameOver){
      drawMessage('Game Over', 'クリックまたはマウスでリスタート');
    } else if(gameClear){
      drawMessage('クリア!', 'クリックまたはマウスでリスタート');
    } else if(paused){
      drawMessage('START', 'クリックまたはスペースで開始');
    }

    requestAnimationFrame(loop);
  }

  // init
  resetGame();
  loop();
})();
</script>
</body>
</html>
```