"""Explicitly run on the GPU host for native Windows acceptance testing."""
import argparse
import os
import subprocess
import sys
import time

p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=15);p.add_argument('--child',action='store_true');p.add_argument('--exit-code',type=int,default=0);args=p.parse_args()
if not 0<=args.seconds<=600: p.error('--seconds must be 0..600')
import torch
print(f'python={sys.executable} pid={os.getpid()} cuda={torch.cuda.is_available()}',flush=True)
if not torch.cuda.is_available(): raise SystemExit(4)
x=torch.arange(1024,device='cuda');print(f'gpu={torch.cuda.get_device_name(0)} sum={x.sum().item()}',flush=True)
child=None
if args.child:
    child=subprocess.Popen([sys.executable,'-u','-c','import time; time.sleep(120)'])
    print(f'child_pid={child.pid}',flush=True)
for tick in range(args.seconds):
    print(f'tick={tick}',flush=True);time.sleep(1)
if child is not None:
    child.terminate();child.wait(timeout=10)
raise SystemExit(args.exit_code)
