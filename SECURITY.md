# Security policy

## Reporting a vulnerability

Please **do not open a public issue**, and never post bank data, tokens or logs with real account numbers.
Report it privately through GitHub: **Security → Report a vulnerability** on this repository (a private security
advisory). Include what you found, how to reproduce it and what an attacker gains. You get an answer within a
week; a fix follows once it is confirmed, with credit if you want it.

## Supported versions

Only the latest version on `main` gets fixes.

## What is in scope

Anything that exposes financial data or credentials: reaching the API without the dashboard's token, reading
the Firefly token, bank sessions or Trade Republic login, or injection through imported transactions.

Out of scope: changes or outages at Enable Banking or Trade Republic (pytr is an unofficial API, see the
README), and what you do with your own data.
