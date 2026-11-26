# Cited Privacy Policy

**Effective date:** November 22, 2026

Cited is a search tool for Claude. It finds relevant excerpts from public newsletters, Medium publications, and RSS/podcast feeds. This policy explains what data the hosted Cited service collects and what happens to it.

## The short version

- Cited has **no accounts and no sign-in**. We don't ask for your name, email, or any personal details.
- When you search, the hosted service **logs the newsletter URL and the question you asked**, so the operator can understand usage and improve the service.
- Logs are **deleted automatically after 90 days** or sooner, are **never sold**, and are visible only to the operator.

## What we collect

When Claude uses the hosted Cited service (`https://cited-7bys.onrender.com/mcp`) on your behalf, we receive and may store:

| Data | Why |
|---|---|
| The newsletter or feed URL you searched | To run the search and understand which sources people use |
| The question or search terms | To run the search and improve result quality |
| The top matching excerpt and source titles/links returned | To review result quality |
| A retrieval confidence score and timestamp | To measure how well search is working |

Our hosting provider (Render) also keeps standard server logs. These include the IP address of the client connecting to the service, the time, and the page requested. They do not include your question. Note that when you use Cited through Claude, the connection usually comes from Anthropic's servers, not your device.

**We do not collect:** names, email addresses, passwords, payment information, or the rest of your Claude conversation. Cited only sees what Claude sends in a search request.

### Error and security logs

To keep the service working and secure, the hosted service also keeps an error log. It records technical problems (for example "couldn't fetch this feed"), blocked attacks (for example a request to fetch a private network address), and rate-limit hits. These entries may include the newsletter or feed URL involved and the error message. They **do not include your question or your IP address**. Error log entries are deleted automatically after 30 days.

### Paid subscriptions you connect

If you add a creator's member key to your Cited connector (for example, a podcast's members-only search), Cited uses that key **only for your request**: it passes it to that creator's own server to search on your behalf and then forgets it. Your key is never stored, logged, or sent anywhere else. When a subscription is connected, **your search question is also sent to that creator's server** — their privacy policy covers what they do with it. If you don't connect a subscription, nothing is sent to any creator.

### Blocking abuse

If a client repeatedly tries attacks (for example, many requests for private network addresses) the service may block its IP address for up to 60 minutes. That IP address is held **only in the server's memory** for the length of the block; it is never written to disk, logs we keep, or reports.

### Usage counts

To count roughly how many different clients use the service each day, the server turns each IP address into a scrambled code using a secret key that exists only in memory and is replaced every day. The code can't be turned back into an IP address or linked across days, and the day's codes are deleted once the day ends — only the daily total is kept.

### Daily summary to the operator

The operator receives a daily email summary of the service's health. It contains counts only (number of searches, errors, blocked attacks) and error descriptions — never your questions or IP address.

## How we use it

- To answer your search request.
- To understand usage (for example, which newsletters are popular) and to fix problems and improve search quality.
- To detect and block abuse and attacks against the service.

We do not use this data for advertising, and we do not try to identify you.

## Who can see it

- **The Cited operator.** Logged searches can be read only with a private administrator key.
- **Service providers.** Render (hosting) stores the data on our behalf.
- **Content sources.** To run a search, the Cited server requests public posts from the newsletter's host (for example, Substack or Medium). Those requests come from our server, not your device, and don't include your question.

We **do not sell or rent** your data. We may disclose data if required by law.

If Cited is sold, merged, or transferred to a new owner, the logged data may be transferred as part of that transaction. The new owner must honor this policy for data collected under it, or give notice before making material changes.

## How long we keep it

Logged searches are **automatically deleted after 90 days**. They may be deleted sooner, for example when the server is redeployed. We do not keep backups of search logs.

## Your choices

- **Don't want your searches logged?** Run Cited locally instead of using the hosted service. Local installs store history only on your own computer. See the [README](README.md).
- **Want a search deleted?** Contact us (below) with the approximate time and the question, and we'll delete matching entries.
- Don't put sensitive personal information in your search questions.

## Running Cited yourself

If you install Cited on your own computer (the local, "stdio" mode), your search history is stored only in a file on your machine and is never sent to us. If you enable the optional `ask_newsletter` tool, your questions and the matching excerpts are sent to Anthropic's API using **your own** API key, under Anthropic's terms.

## Children

Cited is not directed at children under 13, and we do not knowingly collect data from them.

## Changes

If we change this policy, we'll update the effective date above. Material changes will be noted in the project's GitHub repository.

## Contact

Questions or deletion requests: **[CONTACT EMAIL — fill in before publishing]**, or open an issue at <https://github.com/drLIyer/Cited/issues> (please don't include personal information in a public issue).
