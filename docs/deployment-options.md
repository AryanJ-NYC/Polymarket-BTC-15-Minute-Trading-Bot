# Deployment options

Researched October 21, 2026. Prices below are steady-state USD before tax;
promotional credits and trials are excluded. No infrastructure was created.

## Recommendation

Use a **DigitalOcean Basic Regular Droplet: $6/month**, 1 GiB RAM, 1 vCPU,
25 GiB SSD, and included public IPv4. Optional weekly backups bring this to
**$7.20/month**. This runs the existing Python process and SQLite journal directly
without adding a database service or container platform. If minimizing the bill
is the priority, **Vultr's comparable 1 GB instance is $5/month**, or $6 with
automatic backups: a $12/year saving before backups.
[DigitalOcean pricing](https://www.digitalocean.com/pricing/droplets),
[DigitalOcean backups](https://docs.digitalocean.com/products/backups/details/pricing/),
[Vultr plans API](https://api.vultr.com/v2/plans?type=all),
[Vultr backup charges](https://docs.vultr.com/support/platform/billing/how-much-does-it-cost-to-enable-automatic-backups)

Start with paper mode. A 1 GB VM is a reasonable sizing estimate for this small
Python/HTTP workload, **not a measured memory requirement**. Measure RAM, CPU,
disk growth, and API latency before considering a 512 MB downgrade. Real-money
execution and recovery still need separate validation; hosting does not establish
strategy profitability or live-trading readiness.

## Monthly comparison

The VM options include persistent root disks. Managed services need an explicit
persistent volume for SQLite. Costs assume one continuously running instance.

| Platform | Practical configuration and recurring price | Fit and additional costs |
| --- | --- | --- |
| **DigitalOcean Droplets** | **$6**, 1 GiB / 1 vCPU / 25 GiB SSD / 1,000 GiB transfer. Smaller option: **$4**, 512 MiB / 10 GiB / 500 GiB transfer. | Straightforward Linux VM; IPv4 included for these bundled plans. Weekly backups +20%; daily +30%. No separate volume needed initially. [Pricing](https://www.digitalocean.com/pricing/droplets), [backups](https://docs.digitalocean.com/products/backups/details/pricing/) |
| **Vultr Cloud Compute** | **$5**, `vc2-1c-1gb`: 1 GB / 1 vCPU / 25 GB SSD / 1 TB transfer. **$3.50** 512 MB IPv4 plan currently lists New Jersey (`ewr`) only. | Cheapest compared 1 GB VPS. The official API lists multiple US locations for the $5 plan. Backups +20%. The $2.50 `-v6` plan is excluded because dependency reachability over IPv6 has not been verified. [Live plans catalog](https://api.vultr.com/v2/plans?type=all), [backup pricing](https://docs.vultr.com/support/platform/billing/how-much-does-it-cost-to-enable-automatic-backups) |
| **AWS Lightsail** | **$7**, Linux IPv4 bundle: 1 GB / 2 vCPU / 40 GB SSD / 2 TB transfer. **$5** for 512 MB / 20 GB / 1 TB. | Predictable VM bill; use a US region. Public IPv4 is included in these bundle prices. Snapshots and transfer overages are additional; do not substitute the cheaper IPv6-only bundle without testing connectivity. [Official bundles](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html), [pricing and extras](https://aws.amazon.com/lightsail/pricing/) |
| **Railway** | Hobby **$5 minimum**, including $5 usage. RAM $10/GB-month, CPU $20/vCPU-month, volume $0.15/GB-month, outbound $0.05/GB. | Git deployment with an always-running service and volume. Example: average 0.5 GB RAM + 0.02 vCPU + 1 GB storage ≈ **$5.55/month**, plus egress. At 1 GB + 0.05 vCPU: ≈ **$11.15**. These are assumptions, not this bot's measured bill. [Billing model](https://docs.railway.com/pricing), [volume limits](https://docs.railway.com/volumes/reference) |
| **Render Background Worker** | **$7**, 512 MB worker + **$0.25** for 1 GB persistent disk = **$7.25** on the $0 Hobby workspace. Next established worker size is 2 GB at $25 + disk. | Continuous background process, no HTTP server required. 512 MB needs a memory check. Files outside the mounted disk disappear on replacement. Bandwidth over the Hobby allowance is additional. [Pricing](https://render.com/pricing), [worker pricing example](https://render.com/articles/production-rails-hosting-guide), [persistent disks](https://render.com/docs/disks) |
| **Fly.io** | Ashburn/Secaucus: 1 GB shared-CPU Machine **$6.70** per 30 days + 1 GB volume **$0.15** = **$6.85**, plus egress. 512 MB + volume: **$3.84**. | Container deployment with a local persistent volume. North American internet egress $0.02/GB. No dedicated ingress IP is needed; optional static egress IP costs about $3.60/month. Keep auto-stop disabled. 31-day compute costs are higher. [Official resource pricing](https://docs.fly.io/about/pricing) |
| **Hetzner Cloud, US** | Published US CPX11 example: **$20.49 + $0.60 IPv4 = $21.09**; 2 vCPU / 2 GB / 40 GB. | The June 15, 2026 adjustment's **USA (ASH/HIL), new USD price** is $20.49, excluding IPv4. This is not the old $6.99 figure or an EU advertised minimum. Confirm current console pricing and capacity before purchase. At this published price, unattractive for this bot. [Dated price table](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [US configuration](https://www.hetzner.com/cloud/regular-performance/), [IPv4 fee](https://docs.hetzner.com/cloud/servers/primary-ips/overview/) |

## Operating shape

- Run **one process on one host**, with exclusive use of the trading account.
  The journal's POSIX file lock coordinates processes using the same file;
  separate hosts or journals can still overlap. Preserve `logs/`, including
  SQLite state, across releases. See [runner](../bot.py) and
  [journal](../execution/trade_journal.py).
- A VPS needs a Python virtual environment and a service manager. Preserve fatal
  stops for inspection instead of configuring an unconditional restart loop.
  Protect `.env.us` as a private file and pass `--env-file` when appropriate.
  No public web endpoint, load balancer, managed database, or Redis is needed.
- On Railway, use one service with a volume mounted at the repo's `logs/` path;
  disable Serverless sleep. Volume-backed releases have brief downtime and
  cannot use replicas. Railway offers US East in Virginia.
  [Volume behavior](https://docs.railway.com/volumes/reference),
  [sleep behavior](https://docs.railway.com/guides/cut-idle-costs-serverless),
  [regions](https://docs.railway.com/deployments/regions)
- On Render, use a paid Background Worker and mount its disk at `logs/`.
  US Virginia, Ohio, and Oregon regions are available.
  [Worker behavior](https://render.com/docs/background-workers),
  [regions](https://render.com/docs/regions)
- **DigitalOcean App Platform is a different product from Droplets.** Its local
  filesystem is ephemeral and it does not support persistent volumes, so it
  would require changing this bot's storage architecture.
  [App Platform storage limits](https://docs.digitalocean.com/products/app-platform/how-to/store-data/)

Budget separately for retained backups and logs. Test authenticated reads and
paper operation from the chosen host: no cloud region's Polymarket US network
access, latency, or suitability for this account was tested during this research.
