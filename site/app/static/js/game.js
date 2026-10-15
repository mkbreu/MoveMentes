// app/static/js/game.js
(function () {
    'use strict';

    const gameContainer = document.querySelector('.game-container');
    if (!gameContainer) {
        return;
    }

    const runner = gameContainer.querySelector('.runner');
    const startMessage = gameContainer.querySelector('.game-start-message');
    const scoreDisplay = gameContainer.querySelector('.score-display');
    const bestScoreDisplay = gameContainer.querySelector('.best-score-display');
    const speedDisplay = gameContainer.querySelector('.speed-display');

    const RUNNER_BOTTOM = 10;
    const JUMP_HEIGHT = 78;
    const BEST_SCORE_KEY = 'movimenteGameBestScoreV2';

    let isJumping = false;
    let runnerPosition = RUNNER_BOTTOM;
    let score = 0;
    let gameStarted = false;
    let obstacles = [];
    let gameLoopId = null;
    let spawnTimer = null;
    let currentSpeed = 3.2;

    function getBestScore() {
        try {
            return parseInt(localStorage.getItem(BEST_SCORE_KEY) || '0', 10) || 0;
        } catch (error) {
            return 0;
        }
    }

    function setBestScore(value) {
        try {
            localStorage.setItem(BEST_SCORE_KEY, String(value));
        } catch (error) {
            // localStorage pode estar bloqueado; o jogo continua sem historico persistente.
        }
    }

    function randomBetween(min, max) {
        return min + Math.random() * (max - min);
    }

    function updateScore() {
        if (scoreDisplay) {
            scoreDisplay.textContent = `Pontos: ${score}`;
        }
        if (bestScoreDisplay) {
            bestScoreDisplay.textContent = `Recorde: ${getBestScore()}`;
        }
        if (speedDisplay) {
            speedDisplay.textContent = `Vel: ${currentSpeed.toFixed(1)}x`;
        }
    }

    function clearObstacles() {
        obstacles.forEach(function (obstacle) {
            obstacle.remove();
        });
        obstacles = [];
    }

    function resetRunner() {
        runnerPosition = RUNNER_BOTTOM;
        isJumping = false;
        if (runner) {
            runner.style.bottom = `${RUNNER_BOTTOM}px`;
            runner.style.display = 'block';
        }
    }

    function scheduleNextObstacle() {
        if (!gameStarted) {
            return;
        }

        const baseDelay = score < 6 ? randomBetween(1350, 1750) : randomBetween(850, 1600);
        spawnTimer = setTimeout(function () {
            generateObstacle();
            scheduleNextObstacle();
        }, baseDelay);
    }

    function startGame() {
        score = 0;
        currentSpeed = 3.2;
        gameStarted = true;

        resetRunner();
        clearObstacles();
        if (startMessage) {
            startMessage.style.display = 'none';
        }

        if (spawnTimer) {
            clearTimeout(spawnTimer);
        }
        updateScore();
        scheduleNextObstacle();
        gameLoop();
    }

    function jump() {
        if (isJumping || !gameStarted || !runner) {
            return;
        }

        isJumping = true;
        const jumpSpeed = 6;

        const upInterval = setInterval(function () {
            if (runnerPosition >= JUMP_HEIGHT) {
                clearInterval(upInterval);
                const downInterval = setInterval(function () {
                    if (runnerPosition <= RUNNER_BOTTOM) {
                        clearInterval(downInterval);
                        runnerPosition = RUNNER_BOTTOM;
                        runner.style.bottom = `${RUNNER_BOTTOM}px`;
                        isJumping = false;
                        return;
                    }
                    runnerPosition -= jumpSpeed;
                    runner.style.bottom = `${runnerPosition}px`;
                }, 18);
                return;
            }

            runnerPosition += jumpSpeed;
            runner.style.bottom = `${runnerPosition}px`;
        }, 18);
    }

    function chooseObstacleType() {
        if (score < 7) {
            return 'cactus';
        }
        return Math.random() < 0.34 ? 'bird' : 'cactus';
    }

    function generateObstacle() {
        if (!gameStarted) {
            return;
        }

        const type = chooseObstacleType();
        const obstacle = document.createElement('div');
        obstacle.className = `obstacle ${type}`;
        obstacle.dataset.type = type;
        obstacle.style.left = `${gameContainer.clientWidth + 30}px`;

        if (type === 'bird') {
            obstacle.style.bottom = `${randomBetween(54, 76)}px`;
        }

        gameContainer.appendChild(obstacle);
        obstacles.push(obstacle);
    }

    function updateDifficulty() {
        if (score <= 7) {
            currentSpeed = 3.2 + (score * 0.34);
        } else {
            currentSpeed = Math.random() < 0.55
                ? randomBetween(3.8, 5.1)
                : randomBetween(6.1, 8.4);
        }
        updateScore();
    }

    function rectsCollide(a, b) {
        const pad = 5;
        return (
            a.left + pad < b.right - pad &&
            a.right - pad > b.left + pad &&
            a.top + pad < b.bottom - pad &&
            a.bottom - pad > b.top + pad
        );
    }

    function gameLoop() {
        if (!gameStarted) {
            if (gameLoopId) {
                cancelAnimationFrame(gameLoopId);
            }
            return;
        }

        const runnerRect = runner ? runner.getBoundingClientRect() : null;

        for (let index = obstacles.length - 1; index >= 0; index--) {
            const obstacle = obstacles[index];
            const obstacleLeft = parseFloat(obstacle.style.left || '0');

            if (obstacleLeft < -70) {
                obstacle.remove();
                obstacles.splice(index, 1);
                score += 1;
                updateDifficulty();
                continue;
            }

            obstacle.style.left = `${obstacleLeft - currentSpeed}px`;

            if (runnerRect && rectsCollide(runnerRect, obstacle.getBoundingClientRect())) {
                gameOver();
                return;
            }
        }

        gameLoopId = requestAnimationFrame(gameLoop);
    }

    function gameOver() {
        gameStarted = false;
        if (spawnTimer) {
            clearTimeout(spawnTimer);
            spawnTimer = null;
        }
        if (gameLoopId) {
            cancelAnimationFrame(gameLoopId);
            gameLoopId = null;
        }

        if (score > getBestScore()) {
            setBestScore(score);
        }
        updateScore();

        if (startMessage) {
            startMessage.textContent = `Fim de jogo. Pontos: ${score}. Recorde: ${getBestScore()}. Toque para jogar de novo.`;
            startMessage.style.display = 'block';
        }
        if (runner) {
            runner.style.display = 'none';
        }
    }

    function handleGameAction(event) {
        event.preventDefault();
        event.stopPropagation();
        gameContainer.focus();

        if (!gameStarted) {
            startGame();
            return;
        }
        jump();
    }

    gameContainer.addEventListener('keydown', function (event) {
        if (event.code === 'Space') {
            handleGameAction(event);
        }
    });

    gameContainer.addEventListener('pointerdown', handleGameAction);
    updateScore();
}());
