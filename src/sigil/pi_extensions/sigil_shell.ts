import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn, spawnSync } from "node:child_process";
import { closeSync, openSync, readSync, statSync, writeSync } from "node:fs";
import { Type } from "typebox";

const DEFAULT_CAPTURE_BYTES = 6000;
const READ_SIZE = 1;

class TailBuffer {
	private limit: number;
	private chunks: Buffer[] = [];
	private size = 0;

	constructor(limit: number) {
		this.limit = Math.max(0, limit);
	}

	append(chunk: Buffer): void {
		if (this.limit === 0 || chunk.length === 0) return;
		this.chunks.push(Buffer.from(chunk));
		this.size += chunk.length;
		while (this.size > this.limit && this.chunks.length > 0) {
			const overflow = this.size - this.limit;
			const first = this.chunks[0];
			if (first.length <= overflow) {
				this.chunks.shift();
				this.size -= first.length;
				continue;
			}
			this.chunks[0] = first.subarray(overflow);
			this.size -= overflow;
			break;
		}
	}

	text(): string {
		return Buffer.concat(this.chunks, this.size).toString("utf8");
	}
}

function configuredCaptureBytes(): number {
	const raw = process.env.SIGIL_RUN_CAPTURE_BYTES ?? String(DEFAULT_CAPTURE_BYTES);
	const parsed = Number.parseInt(raw, 10);
	return Number.isFinite(parsed) ? Math.max(0, parsed) : DEFAULT_CAPTURE_BYTES;
}

function terminalFd(): { fd: number; close: boolean } | null {
	const raw = process.env.SIGIL_TTY_FD;
	if (raw) {
		const fd = Number.parseInt(raw, 10);
		if (Number.isInteger(fd) && fd >= 0) {
			try {
				statSync(`/dev/fd/${fd}`);
				return { fd, close: false };
			} catch {
				// Fall back to opening /dev/tty below.
			}
		}
	}
	try {
		return { fd: openSync("/dev/tty", "r+"), close: true };
	} catch {
		return null;
	}
}

function writeTerminal(fd: number, value: string | Buffer): void {
	try {
		writeSync(fd, value);
	} catch {
		// Terminal output is best-effort; the tool result still carries details.
	}
}

function closeTerminal(terminal: { fd: number; close: boolean }): void {
	if (!terminal.close) return;
	try {
		closeSync(terminal.fd);
	} catch {
		// Nothing useful to do during cleanup.
	}
}

function readTerminalLine(fd: number, prompt: string): string {
	writeTerminal(fd, prompt);
	const chunks: Buffer[] = [];
	const byte = Buffer.alloc(READ_SIZE);
	while (true) {
		const count = readSync(fd, byte, 0, READ_SIZE, null);
		if (count === 0) break;
		if (byte[0] === 10 || byte[0] === 13) break;
		chunks.push(Buffer.from(byte.subarray(0, count)));
	}
	return Buffer.concat(chunks).toString("utf8").trim();
}

function commandFrom(params: { command?: string; cmd?: string }): string {
	return String(params.command ?? params.cmd ?? "").trim();
}

function sigilBin(): string {
	return process.env.SIGIL_BIN || "sigil";
}

function recordTurn(command: string, status: number, stdout: string, stderr: string): void {
	const args = ["record-turn", "--status", String(status), "--cwd", process.cwd()];
	if (stdout) args.push("--stdout-snippet", stdout);
	if (stderr) args.push("--stderr-snippet", stderr);
	args.push(command);
	spawnSync(sigilBin(), args, { stdio: "ignore" });
}

async function runCommand(
	command: string,
	fd: number,
	timeoutSeconds?: number,
): Promise<{ exitCode: number; stdout: string; stderr: string; timedOut: boolean }> {
	const stdoutTail = new TailBuffer(configuredCaptureBytes());
	const stderrTail = new TailBuffer(configuredCaptureBytes());
	const shell = process.env.SHELL || "/bin/sh";
	const child = spawn(shell, ["-lc", command], {
		cwd: process.cwd(),
		env: process.env,
		stdio: ["ignore", "pipe", "pipe"],
	});

	let timedOut = false;
	let timer: NodeJS.Timeout | undefined;
	if (timeoutSeconds !== undefined && timeoutSeconds > 0) {
		timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGTERM");
		}, timeoutSeconds * 1000);
	}

	child.stdout.on("data", (chunk: Buffer) => {
		stdoutTail.append(chunk);
		writeTerminal(fd, chunk);
	});
	child.stderr.on("data", (chunk: Buffer) => {
		stderrTail.append(chunk);
		writeTerminal(fd, chunk);
	});

	const exitCode = await new Promise<number>((resolve) => {
		child.on("error", () => resolve(127));
		child.on("close", (code, signal) => {
			if (timer) clearTimeout(timer);
			if (code !== null) {
				resolve(code);
				return;
			}
			resolve(signal ? 128 : 1);
		});
	});

	return {
		exitCode,
		stdout: stdoutTail.text(),
		stderr: stderrTail.text(),
		timedOut,
	};
}

function resultText(command: string, exitCode: number, stdout: string, stderr: string, timedOut: boolean): string {
	const parts = [`Command: ${command}`, `Exit status: ${exitCode}`];
	if (timedOut) parts.push("Timed out: yes");
	parts.push("Stdout:");
	parts.push(stdout || "<empty>");
	parts.push("Stderr:");
	parts.push(stderr || "<empty>");
	return parts.join("\n");
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "sigil_shell",
		label: "sigil shell",
		description:
			"Ask the user before running a shell command, stream stdout/stderr to the terminal, and return captured output plus exit status.",
		parameters: Type.Object({
			command: Type.String({ description: "Shell command to propose to the user" }),
			timeout: Type.Optional(Type.Number({ description: "Timeout in seconds" })),
		}),
		async execute(_toolCallId, params: { command?: string; timeout?: number }) {
			const originalCommand = commandFrom(params);
			if (!originalCommand) {
				return { content: [{ type: "text", text: "No command was provided." }], details: { exitCode: 2 } };
			}

			const terminal = terminalFd();
			if (terminal === null) {
				return {
					content: [{ type: "text", text: `No terminal is available to approve command:\n${originalCommand}` }],
					details: { exitCode: 2, command: originalCommand },
				};
			}

			try {
				let command = originalCommand;
				writeTerminal(terminal.fd, `\n❯ proposed command\n${command}\n`);
				const decision = readTerminalLine(terminal.fd, "run? [y/e/N] ").toLowerCase();
				if (decision === "e" || decision === "edit") {
					const edited = readTerminalLine(terminal.fd, "command> ");
					if (edited) command = edited;
				} else if (decision !== "y" && decision !== "yes") {
					return {
						content: [{ type: "text", text: `Command declined by user:\n${command}` }],
						details: { declined: true, command },
					};
				}

				writeTerminal(terminal.fd, `\n❯ running\n${command}\n\n`);
				const result = await runCommand(command, terminal.fd, params.timeout);
				writeTerminal(terminal.fd, `\n❯ exit ${result.exitCode}\n`);
				recordTurn(command, result.exitCode, result.stdout, result.stderr);

				return {
					content: [
						{
							type: "text",
							text: resultText(command, result.exitCode, result.stdout, result.stderr, result.timedOut),
						},
					],
					details: {
						command,
						exitCode: result.exitCode,
						stdout: result.stdout,
						stderr: result.stderr,
						timedOut: result.timedOut,
					},
				};
			} finally {
				closeTerminal(terminal);
			}
		},
	});
}
