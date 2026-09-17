# Security

## What this program touches

cclens reads the JSONL files under the `.claude` directories it is pointed at,
and writes one SQLite index. It never writes to a source file.

It makes no outbound network requests, has no telemetry, loads no remote
scripts, fonts or stylesheets, and needs no credentials.

## The server

`cclens serve` binds `127.0.0.1` by default and sets no CORS headers, so a
page on another origin cannot read from it. It also answers only to a request
whose `Host` header names loopback, which is what stops a page from reaching it
by pointing a name it owns at `127.0.0.1`. There is no authentication: anything
that can reach the port and name it as localhost can read your logs. Binding a
non-loopback address lifts the `Host` requirement, prints a warning, and is
your decision.

Responses carry `Content-Security-Policy: default-src 'none'` with `'self'`
for scripts and styles, so the page cannot load or contact anything else.

Static files are served by exact name from the package directory. Raw lines
are read only from paths already recorded in the index, so a request cannot
name a file of its own.

`/api/doctor` reports the index location and the roots being read, which are
absolute paths in your home directory.

## Your logs are not anonymous

Transcripts contain whatever you and the model said, including file contents,
command output, and anything a tool returned. The index holds a summary line
and a search projection of the same text. Treat the index as being as
sensitive as the transcripts, and keep it out of any repository: the shipped
`.gitignore` covers `*.db` and `*.jsonl`.

## Reporting

Open an issue, or for anything you would rather not post publicly, use GitHub
private vulnerability reporting on this repository.
