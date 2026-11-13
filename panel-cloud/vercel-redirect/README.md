# speexx.tykunanon.online → the panel's ngrok tunnel

This Vercel project exists only to answer `speexx.tykunanon.online` with a redirect to
the ngrok tunnel the panel starts. It deploys `vercel.json` and nothing else.

## Why a redirect and not a tunnel

`tykunanon.online`'s nameservers are `ns1/ns2.vercel-dns.com`, so Vercel is
authoritative for DNS but cannot reach a service on this machine. A Cloudflare Tunnel
needs Cloudflare to be authoritative, which it is not — measured 2026-10-05, see
`../AGENTS.md`.

## What this does and does not fix

It gives a short, memorable URL. It does **not** remove ngrok's `ERR_NGROK_6024`
browser warning, and because it is a redirect the URL bar shows the ngrok domain. For
either of those, the options are Tailscale Funnel or ngrok's paid custom domain.

## permanent: false is deliberate

A 308 is cached by browsers for a long time. If the ngrok URL changes, everyone holding
a cached redirect is stranded with no way past it. Keep this `false`.

## Changing the destination

Edit `vercel.json`, then:

```bash
vercel --prod
```

The tunnel has to be running (`./run.sh --with-tunnel` in `..`) or the redirect lands on
ngrok's error page.
