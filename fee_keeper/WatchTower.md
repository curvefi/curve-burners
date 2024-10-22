# CowSwap WatchTower
[WatchTower](https://github.com/cowprotocol/watch-tower)
## Instructions
### Build docker
```shell
docker build -t watch-tower .
```
Create volume to reuse processed orders
```shell
docker volume create watch-tower-eth
```

### Config
Add `config.json` into `watch-tower/` directory
```json
{
    "networks": [
        {
            "name": "mainnet",
            "rpc": "https://ovh.nodes.cow.fi/mainnet",
            "deploymentBlock": 20160887,
            "watchdogTimeout": 120,
            "processEveryNumBlocks": 10,
            "filterPolicy": {
                "defaultAction": "DROP",
                "owners": {
                    "0xC0fC3dDfec95ca45A0D2393F518D3EA1ccF44f8b": "ACCEPT"
                },
                "handlers": {
                    "0xC0fC3dDfec95ca45A0D2393F518D3EA1ccF44f8b": "ACCEPT"
                }
            }
        }
    ]
}
```
| Parameter                 | Comment                                                                                                                              |
|---------------------------|:-------------------------------------------------------------------------------------------------------------------------------------|
| **rpc**                   | Not all RPCs are supported. Discovered to be compatible are: ovh, alchemy and infura(Geth/v1.14.\*). But NOT drpc or erigon/v2.60.\* |
| **deploymentBlock**       | Block number of `CowSwapBurner` deployment, will go over all blocks fetching orders' data from events                                |
| **watchdogTimeout**       | Public RPCs may lag, set 2 minutes for response                                                                                      |                                                                                                                           
| **processEveryNumBlocks** | Some coins' balance is increased over time, so WatchTower creates a new order every block. Too much orders can lead to API ban       |
| **filterPolicy**          | Handle orders related only to Curve fee burning                                                                                      |

### Run
```shell
docker run -it -d \
  -v "~/watch-tower/config.json:/config.json" \
  -v watch-tower-eth:/usr/src/app/database \
  watch-tower:latest \
  run \
  --config-path /config.json
```
Set `~/watch-tower/` to location of your `watch-tower` directory.

At first execution it will take a few hours to catch up with the last block.
Then, it will use volume to go after last processed block.

### Scheduling
Tower should be run on Tuesdays 00:00-23:59 UTC, though even few hours is okay.

This is an example of set up using systemd scheduling.
Add following files to `/etc/systemd/system/`

**watch-tower-run.service**
```ini
[Unit]
Description="CoWSwap WatchTower run"

[Service]
ExecStart=/usr/bin/docker run -it -d \
    -v "<watch-tower repo>/config.json:/config.json" \
    -v watch-tower-eth:/usr/src/app/database \
    watch-tower:latest \
    run \
    --config-path /config.json

[Install]
WantedBy=multi-user.target
```

**watch-tower-run.timer**
```ini
[Unit]
Description="Run docker container, that posts orders to exchange fees"

[Timer]
OnCalendar=Tue *-*-* 00:15:00 UTC
Persistent=true
    
[Install]
WantedBy=timers.target
```

**watch-tower-stop.service**
```ini
[Unit]
Description="CoWSwap WatchTower stop"

[Service]
ExecStart=/bin/sh -c 'docker rm -f $(docker ps | grep watch-tower:latest | awk '\''{print $1}'\'')'

[Install]
WantedBy=multi-user.target
```

**watch-tower-stop.timer**
```ini
[Unit]
Description="Stop and remove docker container, that posts orders to exchange fees"

[Timer]
OnCalendar=Tue *-*-* 23:45:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start timers
```shell
sudo systemctl daemon-reload
sudo systemctl enable watch-tower-run.timer
sudo systemctl start watch-tower-run.timer
```
```shell
sudo systemctl daemon-reload
sudo systemctl enable watch-tower-stop.timer
sudo systemctl start watch-tower-stop.timer
```

To see logs of task run `journalctl -u watch-tower-run.timer`.
To list all timers call `systemctl list-timers --all`.
