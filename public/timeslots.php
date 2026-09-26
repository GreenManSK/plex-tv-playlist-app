<?php
// ===== Timeslots & Playlist Generator =====
declare(strict_types=1);

require __DIR__ . '/_bootstrap.php';
require __DIR__ . '/_csrf.php';

// Paths
$ROOT = realpath(__DIR__ . '/..');
$dbFilePath = $ROOT . '/database/plex_playlist.db';

// Scripts (names only; helpers prepend /scripts)
$getEpisodesScript       = 'getEpisodes.py';
$newPlaylistScript       = 'newPlaylist.py';
$generatePlaylistScript  = 'generatePlaylist.py';

// Logs
$logDir = $ROOT . '/logs';
if (!is_dir($logDir)) { @mkdir($logDir, 0775, true); }
$timestamp = date('Ymd_His');
$log_getEpisodes      = "$logDir/getEpisodes_$timestamp.log";
$log_newPlaylist      = "$logDir/newPlaylist_$timestamp.log";
$log_generatePlaylist = "$logDir/generatePlaylist_$timestamp.log";

// ---- DB connect (to list shows & set timeslots) ----
try {
    $conn = new PDO("sqlite:$dbFilePath");
    $conn->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
} catch (PDOException $e) {
    die("Connection failed: " . htmlspecialchars((string)$e->getMessage(), ENT_QUOTES, 'UTF-8'));
}

// Safety net for databases created before slotPriority existed.
foreach (['playlistShows', 'playlistEpisodes'] as $table) {
    $cols = $conn->query("PRAGMA table_info($table)")->fetchAll(PDO::FETCH_COLUMN, 1);
    if (!in_array('slotPriority', $cols, true)) {
        $conn->exec("ALTER TABLE $table ADD COLUMN slotPriority INTEGER DEFAULT 1");
    }
}

$shouldRunPipeline = false;
$error = '';
$notice = '';

$conn->exec("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)");

function setting_get(PDO $conn, string $key): string {
    $stmt = $conn->prepare("SELECT value FROM settings WHERE key = ?");
    $stmt->execute([$key]);
    return (string)($stmt->fetchColumn() ?: '');
}

function setting_set(PDO $conn, string $key, string $value): void {
    $conn->prepare("INSERT INTO settings(key, value) VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value")
         ->execute([$key, $value]);
}

$skipWatched  = setting_get($conn, 'skip_watched') === '1';
$watchedUser  = setting_get($conn, 'watched_user');
$plexUsers    = json_decode(setting_get($conn, 'plex_users') ?: '[]', true);
if (!is_array($plexUsers)) { $plexUsers = []; }

// Refresh the cached list of Plex Home users
if ($_SERVER["REQUEST_METHOD"] === "POST" && ($_POST['action'] ?? '') === 'refresh_users') {
    csrf_validate();

    $r = run_py_logged('listUsers.py', [], "$logDir/listUsers_$timestamp.log");
    $j = json_decode((string)$r['stdout'], true);
    if ($r['exit_code'] === 0 && is_array($j) && !empty($j['ok']) && is_array($j['users'] ?? null)) {
        $plexUsers = $j['users'];
        setting_set($conn, 'plex_users', json_encode($plexUsers));
        $notice = 'Loaded ' . count($plexUsers) . ' Plex user(s).';
    } else {
        $error = 'Could not load Plex users: ' . (is_array($j) ? ($j['error'] ?? 'unknown error') : 'unexpected output');
    }
}

// On POST, update timeslots
if ($_SERVER["REQUEST_METHOD"] === "POST" && isset($_POST['timeslots'])) {
    csrf_validate();

    $skipWatched = !empty($_POST['skip_watched']);
    setting_set($conn, 'skip_watched', $skipWatched ? '1' : '0');

    $watchedUser = trim((string)($_POST['watched_user'] ?? ''));
    setting_set($conn, 'watched_user', $watchedUser);

    // Ensure we have arrays
    $timeslots  = is_array($_POST['timeslots']) ? $_POST['timeslots'] : [];
    $priorities = (isset($_POST['priorities']) && is_array($_POST['priorities'])) ? $_POST['priorities'] : [];

    // Shows may share a timeslot, but the (slot, priority) pair must be unique:
    // within a slot the lower priority plays all its episodes first.
    $pairs = [];
    foreach ($timeslots as $showId => $timeslot) {
        $pairs[] = (int)$timeslot . ':' . (int)($priorities[$showId] ?? 1);
    }

    if (count($pairs) !== count(array_unique($pairs))) {
        $error = "Two shows in the same timeslot cannot have the same priority. Give each show in a slot a distinct priority.";
    } else {
        $shouldRunPipeline = true;
        $sql = "UPDATE playlistShows SET timeSlot = ?, slotPriority = ? WHERE id = ?";
        $stmt = $conn->prepare($sql);
        foreach ($timeslots as $showId => $timeslot) {
            $stmt->execute([ (int)$timeslot, (int)($priorities[$showId] ?? 1), (int)$showId ]);
        }
    }
}

// Fetch shows for form, previously assigned ones first and in playback order
$sql = "SELECT id, title, timeSlot, slotPriority, total_episodes
        FROM playlistShows
        ORDER BY (timeSlot IS NULL) ASC, timeSlot ASC, slotPriority ASC, total_episodes ASC";
$stmt = $conn->query($sql);
$shows = $stmt->fetchAll(PDO::FETCH_ASSOC);
$numOfShows = count($shows);

// Keep saved slot/priority; only shows that never got one fall into a free slot.
$usedPairs = [];
foreach ($shows as $show) {
    if (!empty($show['timeSlot'])) {
        $usedPairs[(int)$show['timeSlot'] . ':' . (int)($show['slotPriority'] ?: 1)] = true;
    }
}
foreach ($shows as $index => $show) {
    if (empty($show['timeSlot'])) {
        $slot = 1;
        while (isset($usedPairs["$slot:1"])) { $slot++; }
        $usedPairs["$slot:1"] = true;
        $shows[$index]['timeSlot'] = $slot;
        $shows[$index]['slotPriority'] = 1;
    } elseif (empty($show['slotPriority'])) {
        $shows[$index]['slotPriority'] = 1;
    }
}

// Dropdowns must always be able to show a cached value, even above the show count
$maxOption = $numOfShows;
foreach ($shows as $show) {
    $maxOption = max($maxOption, (int)$show['timeSlot'], (int)$show['slotPriority']);
}
// We won't use $conn after this
$conn = null;

// ---- Helper: run with logging via bootstrap ----
function run_with_logging(string $script, array $args, string $logfile): array {
    return run_py_logged($script, $args, $logfile);
}

// ---- If form validated, run the pipeline ----
if ($shouldRunPipeline) {
    // 1) getEpisodes.py
    $r1 = run_with_logging($getEpisodesScript, [], $log_getEpisodes);
    if ($r1['exit_code'] !== 0) {
        require __DIR__ . '/partials/head.php';
        require __DIR__ . '/partials/nav.php';
        echo "<pre style='color:#c00;'>getEpisodes.py failed (exit {$r1['exit_code']}). See log:\n{$log_getEpisodes}\n\nSTDERR:\n" . htmlspecialchars((string)$r1['stderr'], ENT_QUOTES, 'UTF-8') . "</pre>";
        require __DIR__ . '/partials/footer.php';
        exit;
    }

    // 2) newPlaylist.py -> expect JSON
    $r2 = run_with_logging($newPlaylistScript, [], $log_newPlaylist);
    if ($r2['exit_code'] !== 0) {
        require __DIR__ . '/partials/head.php';
        require __DIR__ . '/partials/nav.php';
        echo "<pre style='color:#c00;'>newPlaylist.py failed (exit {$r2['exit_code']}). See log:\n{$log_newPlaylist}\n\nSTDERR:\n" . htmlspecialchars((string)$r2['stderr'], ENT_QUOTES, 'UTF-8') . "</pre>";
        require __DIR__ . '/partials/footer.php';
        exit;
    }

    $ratingKey = null;
    $json = json_decode($r2['stdout'], true);
    if (is_array($json) && !empty($json['ok']) && !empty($json['ratingKey'])) {
        $ratingKey = (int)$json['ratingKey'];
    }

    if ($ratingKey) {
        // 3) generatePlaylist.py <ratingKey>
        $generateArgs = [ (string)$ratingKey ];
        if ($skipWatched) { $generateArgs[] = '--skip-watched'; }
        $r3 = run_with_logging($generatePlaylistScript, $generateArgs, $log_generatePlaylist);
        if ($r3['exit_code'] !== 0) {
            require __DIR__ . '/partials/head.php';
            require __DIR__ . '/partials/nav.php';
            echo "<pre style='color:#c00;'>generatePlaylist.py failed (exit {$r3['exit_code']}). See log:\n{$log_generatePlaylist}\n\nSTDERR:\n" . htmlspecialchars((string)$r3['stderr'], ENT_QUOTES, 'UTF-8') . "</pre>";
            require __DIR__ . '/partials/footer.php';
            exit;
        }

        // Success
        require __DIR__ . '/partials/head.php';
        require __DIR__ . '/partials/nav.php';
        echo "<script>alert('Playlist Generated in Plex'); window.location.href = '../index.php';</script>";
        require __DIR__ . '/partials/footer.php';
        exit;
    } else {
        require __DIR__ . '/partials/head.php';
        require __DIR__ . '/partials/nav.php';
        echo "<pre style='color:#c00;'>Failed to create new playlist or retrieve its ratingKey.\nSee log for details:\n{$log_newPlaylist}\n\nSTDOUT:\n" . htmlspecialchars((string)$r2['stdout'], ENT_QUOTES, 'UTF-8') . "\n\nSTDERR:\n" . htmlspecialchars((string)$r2['stderr'], ENT_QUOTES, 'UTF-8') . "</pre>";
        require __DIR__ . '/partials/footer.php';
        exit;
    }
}

// ---------- RENDER FORM ----------
require __DIR__ . '/partials/head.php';
require __DIR__ . '/partials/nav.php';
?>
<div class="container py-4">
    <h2 class="mb-3">Assign Timeslots</h2>
    <p class="text-muted">
        Shows may share a timeslot. Within a slot, the show with the lower priority plays all of its
        episodes first, then the next one. Priorities must be unique inside the same slot.
    </p>
    <?php if (!empty($error)): ?>
        <div class="alert alert-danger"><?= htmlspecialchars((string)$error, ENT_QUOTES, 'UTF-8') ?></div>
    <?php endif; ?>
    <?php if (!empty($notice)): ?>
        <div class="alert alert-success"><?= htmlspecialchars((string)$notice, ENT_QUOTES, 'UTF-8') ?></div>
    <?php endif; ?>

    <form id="refresh-users-form" action="timeslots.php" method="post">
        <?= csrf_field() ?>
        <input type="hidden" name="action" value="refresh_users">
    </form>

    <form id="timeslot-form" action="timeslots.php" method="post">
        <?= csrf_field() ?>
        <div class="row fw-bold d-none d-md-flex mb-1">
            <div class="col-6">Show</div>
            <div class="col-3">Timeslot</div>
            <div class="col-3">Priority in slot</div>
        </div>
        <?php foreach ($shows as $index => $show): ?>
            <div class="row align-items-center mb-2">
                <div class="col-6">
                    <span>
                        <?= htmlspecialchars((string)$show['title'], ENT_QUOTES, 'UTF-8') ?>
                        &mdash; Episodes:
                        <?= htmlspecialchars((string)$show['total_episodes'], ENT_QUOTES, 'UTF-8') ?>
                    </span>
                </div>
                <div class="col-3">
                    <select class="form-select form-select-sm" name="timeslots[<?= (int)$show['id'] ?>]">
                        <?php for ($i = 1; $i <= $maxOption; $i++): ?>
                            <option value="<?= $i ?>" <?= ($i === (int)$show['timeSlot']) ? 'selected' : '' ?>><?= $i ?></option>
                        <?php endfor; ?>
                    </select>
                </div>
                <div class="col-3">
                    <select class="form-select form-select-sm" name="priorities[<?= (int)$show['id'] ?>]">
                        <?php for ($i = 1; $i <= $maxOption; $i++): ?>
                            <option value="<?= $i ?>" <?= ($i === (int)$show['slotPriority']) ? 'selected' : '' ?>><?= $i ?></option>
                        <?php endfor; ?>
                    </select>
                </div>
            </div>
        <?php endforeach; ?>
        <div class="form-check mt-3">
            <input class="form-check-input" type="checkbox" value="1" id="skip-watched" name="skip_watched" <?= $skipWatched ? 'checked' : '' ?>>
            <label class="form-check-label" for="skip-watched">
                Skip episodes already watched in Plex
            </label>
        </div>
        <div class="row align-items-end mt-3">
            <div class="col-12 col-md-6">
                <label class="form-label mb-1" for="watched-user">Build playlist for</label>
                <select class="form-select form-select-sm" id="watched-user" name="watched_user">
                    <option value="">Server owner (default)</option>
                    <?php foreach ($plexUsers as $u):
                        if (!empty($u['owner'])) { continue; }
                        $uid = (string)($u['user'] ?? $u['title'] ?? '');
                        if ($uid === '') { continue; }
                    ?>
                        <option value="<?= htmlspecialchars($uid, ENT_QUOTES, 'UTF-8') ?>" <?= ($uid === $watchedUser) ? 'selected' : '' ?>>
                            <?= htmlspecialchars((string)($u['title'] ?? $uid), ENT_QUOTES, 'UTF-8') ?>
                        </option>
                    <?php endforeach; ?>
                </select>
                <div class="form-text">The playlist is created in this user's account and uses their watched history. Only Plex Home users without a PIN can be used.</div>
            </div>
            <div class="col-12 col-md-auto mt-2 mt-md-0">
                <button class="btn btn-outline-secondary btn-sm" type="submit" form="refresh-users-form">Refresh user list</button>
            </div>
        </div>
        <button class="btn btn-success mt-3" type="submit">Generate Playlist</button>
    </form>
</div>
<script>
document.getElementById('timeslot-form').addEventListener('submit', function (e) {
    const slots = [...this.querySelectorAll('select[name^="timeslots"]')];
    const seen = new Set();
    for (const slot of slots) {
        const showId = slot.name.slice(slot.name.indexOf('[') + 1, -1);
        const priority = this.querySelector('select[name="priorities[' + showId + ']"]');
        const pair = slot.value + ':' + (priority ? priority.value : '1');
        if (seen.has(pair)) {
            e.preventDefault();
            alert('Two shows in the same timeslot cannot have the same priority.');
            return;
        }
        seen.add(pair);
    }
});
</script>
<?php require __DIR__ . '/partials/footer.php'; ?>
