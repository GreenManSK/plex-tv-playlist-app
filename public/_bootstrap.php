<?php
// public/_bootstrap.php
// Central helpers for paths and Python execution

$ROOT = realpath(__DIR__ . '/..');
$PY = getenv('PYTHON_EXEC') ?: '/usr/local/bin/python3';

function run_py_logged(string $script, array $args = [], string $logfile = null): array {
    global $ROOT, $PY;

    $cmd = escapeshellarg($PY) . ' ' . escapeshellarg($ROOT . '/scripts/' . $script);
    foreach ($args as $a) {
        $cmd .= ' ' . escapeshellarg((string)$a);
    }

    $descriptorspec = [
        0 => ["pipe", "r"],
        1 => ["pipe", "w"],
        2 => ["pipe", "w"],
    ];

    $process = proc_open($cmd, $descriptorspec, $pipes, $ROOT);
    $result = ['exit_code' => -1, 'stdout' => '', 'stderr' => '', 'cmd' => $cmd];

    if (is_resource($process)) {
        fclose($pipes[0]);
        stream_set_blocking($pipes[1], false);
        stream_set_blocking($pipes[2], false);

        $stdout = '';
        $stderr = '';
        if ($logfile) {
            @file_put_contents($logfile, "=== CMD ===\n$cmd\n\n=== LIVE OUTPUT ===\n");
        }

        while (!feof($pipes[1]) || !feof($pipes[2])) {
            $read = [];
            if (!feof($pipes[1])) { $read[] = $pipes[1]; }
            if (!feof($pipes[2])) { $read[] = $pipes[2]; }
            if (!$read) { break; }

            $write = null;
            $except = null;
            $selected = stream_select($read, $write, $except, 1);
            if ($selected === false) { break; }

            foreach ($read as $stream) {
                $chunk = stream_get_contents($stream);
                if ($chunk === false || $chunk === '') { continue; }

                if ($stream === $pipes[1]) {
                    $stdout .= $chunk;
                } else {
                    $stderr .= $chunk;
                }
                if ($logfile) {
                    @file_put_contents($logfile, $chunk, FILE_APPEND);
                }
                @file_put_contents('php://stderr', $chunk);
            }
        }

        fclose($pipes[1]);
        fclose($pipes[2]);
        $exit = proc_close($process);

        $result['exit_code'] = $exit;
        $result['stdout']    = $stdout;
        $result['stderr']    = $stderr;

        if ($logfile) {
            @file_put_contents($logfile,
                "=== CMD ===\n$cmd\n\n=== EXIT ===\n$exit\n\n=== STDOUT ===\n$stdout\n\n=== STDERR ===\n$stderr\n"
            );
        }
    }
    return $result;
}

function run_py_stdout(string $script, array $args = []): ?string {
    $r = run_py_logged($script, $args);
    return ($r['exit_code'] === 0) ? $r['stdout'] : null;
}
