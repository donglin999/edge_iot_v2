/**
 * Thin wrapper around the ``docker`` CLI used by the chaos specs.
 *
 * The Playwright suite assumes Docker is reachable from the host that
 * runs the tests (mac/Linux dev workstation). All commands log their
 * argv + stdout/stderr to ``test.info().attach`` so the html-report
 * captures broker-stop / broker-start timestamps next to the screenshot.
 */
import { spawn } from 'node:child_process';
import { TestInfo } from '@playwright/test';

export interface DockerRun {
  code: number;
  stdout: string;
  stderr: string;
}

export function dockerCli(args: string[], opts: { timeoutMs?: number } = {}): Promise<DockerRun> {
  return new Promise((resolve, reject) => {
    const proc = spawn('docker', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    proc.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });
    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });
    const timer = opts.timeoutMs
      ? setTimeout(() => {
          proc.kill('SIGKILL');
          reject(new Error(`docker ${args.join(' ')} timed out after ${opts.timeoutMs}ms`));
        }, opts.timeoutMs)
      : null;
    proc.on('error', (err) => {
      if (timer) clearTimeout(timer);
      reject(err);
    });
    proc.on('close', (code) => {
      if (timer) clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

export async function dockerExec(
  container: string,
  cmd: string[],
  info?: TestInfo,
): Promise<DockerRun> {
  const args = ['exec', container, ...cmd];
  const result = await dockerCli(args, { timeoutMs: 30_000 });
  if (info) {
    await info.attach(`docker exec ${container} ${cmd.join(' ')}`, {
      body: `code=${result.code}\n--- stdout ---\n${result.stdout}\n--- stderr ---\n${result.stderr}`,
      contentType: 'text/plain',
    });
  }
  return result;
}

export async function stopContainer(container: string, info?: TestInfo): Promise<DockerRun> {
  const result = await dockerCli(['stop', container], { timeoutMs: 60_000 });
  if (info) {
    await info.attach(`docker stop ${container}`, {
      body: `code=${result.code}\n${result.stdout}${result.stderr}`,
      contentType: 'text/plain',
    });
  }
  return result;
}

export async function startContainer(container: string, info?: TestInfo): Promise<DockerRun> {
  const result = await dockerCli(['start', container], { timeoutMs: 60_000 });
  if (info) {
    await info.attach(`docker start ${container}`, {
      body: `code=${result.code}\n${result.stdout}${result.stderr}`,
      contentType: 'text/plain',
    });
  }
  return result;
}

/**
 * Resolve the running container name for a service whose ``container_name``
 * key may not be predictable (e.g. compose-generated). Returns ``null``
 * when no matching container is up.
 */
export async function findContainerByPrefix(prefix: string): Promise<string | null> {
  const result = await dockerCli(
    ['ps', '--format', '{{.Names}}', '--filter', `name=${prefix}`],
    { timeoutMs: 10_000 },
  );
  if (result.code !== 0) return null;
  const lines = result.stdout
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
  return lines.length ? lines[0] : null;
}
