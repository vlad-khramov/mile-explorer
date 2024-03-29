import asyncio
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal

from dateutil.parser import parse
from sqlalchemy.engine.url import URL

from apps.explorer.indexer_stat.stat import calc_statistics
from apps.explorer.models import Block, Wallet, Transaction
from apps.mileapi.api import get_current_block, get_block, get_wallet, get_wallet_after_block
from apps.mileapi.constants import TX_TYPES, TransferAssetsTransaction, RegisterNodeTransactionWithAmount
from core.collections import unique_deque
from core.config import DB_HOST, DB_USER, DB_PASSWORD, DB_NAME, INDEXER_DB_POOL_MIN_SIZE, INDEXER_DB_POOL_MAX_SIZE, \
    INDEXER_TASKS_LIMIT, \
    GENESIS_BLOCK
from core.di import db
from core.logging import setup_logging

logger = setup_logging('indexer')

last_processed_block_id = 0

TASK_BLOCK = 0
TASK_WALLET = 1
TASKS_NAMES = {TASK_BLOCK: 'block', TASK_WALLET: 'wallet'}


def start(stage=2):
    """stage1 - cold start first-time db fill, stage2 - after db filled"""
    assert stage in (1, 2)

    logger.error('started')

    global last_processed_block_id
    loop = asyncio.get_event_loop()

    dsn = URL(
        drivername='asyncpg', host=DB_HOST, username=DB_USER, password=DB_PASSWORD, database=DB_NAME,
    )

    loop.run_until_complete(
        db.set_bind(
            dsn,
            echo=False,
            min_size=INDEXER_DB_POOL_MIN_SIZE[stage],
            max_size=INDEXER_DB_POOL_MAX_SIZE[stage],
            loop=loop,
        )
    )

    fetch_tasks = unique_deque()

    last_processed_block_id = loop.run_until_complete(process_missing_blocks(fetch_tasks))

    asyncio.ensure_future(check_new_blocks(fetch_tasks))
    asyncio.ensure_future(handle_fetch_tasks(fetch_tasks, stage))  # INDEXER_TASKS_LIMIT connects
    asyncio.ensure_future(check_missing_wallets(fetch_tasks))  # 1 connect
    asyncio.ensure_future(fix_unreal_date())  # 1 connect
    asyncio.ensure_future(calc_statistics())  # 1 connect

    loop.run_forever()


async def check_new_blocks(fetch_tasks: deque):
    global last_processed_block_id
    while True:
        await asyncio.sleep(10)
        try:
            last_block = await get_current_block()
            for block_id in range(last_processed_block_id+1, last_block+1):
                fetch_tasks.append( (TASK_BLOCK, block_id, datetime.now()) )
                logger.info(f"Queued block: {block_id}, tasks: {_count_tasks(fetch_tasks)}")
            last_processed_block_id = last_block
        except:
            pass


async def fix_unreal_date():
    global last_processed_block_id
    while True:
        await asyncio.sleep(20)
        try:
            ids = await db.all("select id, timestamp from blocks where timestamp < '2018-01-01'")
            for block_id in reversed(ids):
                block_id, old_ts = block_id
                async with db.transaction():
                    ts = await db.first(
                        "select timestamp from blocks where id=$1+1 and timestamp > '2018-01-01'",
                        block_id
                    )
                    if not ts:
                        continue
                    ts = ts[0]

                    logger.info(f"Updating unreal ts: block {block_id}, old_ts {old_ts}")
                    await db.status(
                        "update blocks set timestamp_real=timestamp, timestamp=$1::timestamp-interval '20 sec' where id=$2",
                        ts, block_id
                    )
                    await db.status(
                        "update transactions set timestamp_real=timestamp, timestamp=$1::timestamp-interval '20 sec' where block_id=$2",
                        ts, block_id
                    )

        except Exception as e:
            logger.exception(f"fix_unreal_date: {e}")


async def check_missing_wallets(fetch_tasks: deque):
    """since wallets added not in tx(deadlocks), on crash they could not be written with block """
    while True:
        try:

            pub_keys = await db.all("""
                select 
                    tx_wallets.pub_key, ts, block_id
                from 
                    (
                        select 
                            pub_key, min(ts) as ts, max(block_id) as block_id 
                        from 
                            (
                                select wallet_from as pub_key, min(timestamp) as ts, max(block_id) as block_id from transactions where wallet_from is not null group by wallet_from
                                union 
                                select wallet_to as pub_key, min(timestamp) as ts, max(block_id) as block_id from transactions where wallet_to is not null group by wallet_to
                            ) tx_wallets_inner
                        group by pub_key
                    ) tx_wallets 
                    left join wallets on tx_wallets.pub_key=wallets.pub_key
                where wallets.pub_key is null
            """)

            await _process_txs_wallets(fetch_tasks, pub_keys_with_ts_blockid=pub_keys)

        except:
            pass

        await asyncio.sleep(60)


def _count_tasks(fetch_tasks: deque):
    tasks_counts = {}
    for task in fetch_tasks:
        if task[2]<=datetime.now():
            tasks_counts[f"{TASKS_NAMES[task[0]]}_actual"] = tasks_counts.get(f"{TASKS_NAMES[task[0]]}_actual", 0) + 1
        else:
            tasks_counts[f"{TASKS_NAMES[task[0]]}_future"] = tasks_counts.get(f"{TASKS_NAMES[task[0]]}_future", 0) + 1
    return tasks_counts


async def handle_fetch_tasks(fetch_tasks: deque, stage: int):
    futures = []
    _fill_futures(futures, fetch_tasks, stage)

    while True:
        await asyncio.sleep(0.1)
        # print(len(futures), len(block_tasks))
        for f in futures:
            if f.done():
                futures.remove(f)
                if f.exception() or not f.result():
                    logger.warning(f"Error during processing task: {TASKS_NAMES[f.task[0]]}:{f.task[1]}, {f.exception().__class__.__name__}({f.exception()})")
                    fetch_tasks.append(f.task)
        _fill_futures(futures, fetch_tasks, stage)


def _fill_futures(futures: list, fetch_tasks: deque, stage: int):
    actual_tasks = deque([t for t in fetch_tasks if t[2] <= datetime.now()])
    while len(futures) < INDEXER_TASKS_LIMIT[stage] and len(actual_tasks) > 0:
        task = actual_tasks.popleft()
        fetch_tasks.remove(task)
        task_type, item_id, after_time = task

        if task_type==TASK_BLOCK:
            coro = _process_block(item_id, fetch_tasks)
        elif task_type==TASK_WALLET:
            coro = _process_wallet(item_id)
        else:
            assert False
        f = asyncio.ensure_future(coro)

        f.task = (task_type, item_id, after_time)
        futures.append(f)


async def process_missing_blocks(fetch_tasks: deque):

    await _check_genesis_block(fetch_tasks)

    blocks_ids = {row[0] for row in await Block.select('id').where(Block.reindex_needed==False).gino.all()}
    last_block = await get_current_block()

    for block_id in range(1, last_block+1):
        if block_id not in blocks_ids:
            fetch_tasks.append( (TASK_BLOCK, block_id, datetime.now()) )

    wallets = {
        row[0] for row
        in await Wallet.select('pub_key').where(Wallet.valid_before_block.isnot(None)).gino.all()
    }
    for pub_key in wallets:
        fetch_tasks.append( (TASK_WALLET, pub_key, datetime.now()) )
        fetch_tasks.append( (TASK_WALLET, pub_key, datetime.now() + timedelta(minutes=10)) )

    logger.info(f"tasks: {_count_tasks(fetch_tasks)}")

    return last_block


async def _check_genesis_block(fetch_tasks: deque):
    genesis = await Block.get(0)
    if genesis:
        return
    block = Block(
        id=0,
        version=1,
        previous_block_digest='',
        merkle_root='',
        timestamp=datetime(2017, 12, 31, 12, 0, 0),
        transactions_count=0,
        number_of_signers=0,
        round=0,
        block_header_digest='',
        main_signer=''
    )
    txs = []
    with open(GENESIS_BLOCK, 'r') as f:
        for i, line in enumerate(f):
            tx = Transaction(
                digest=f"genesis_{i}",
                block_id=block.id,
                num_in_block=i,
                timestamp=block.timestamp,
                global_num=0,
                is_fee=False,
                fee=0,
                signature='',
                description=''
            )
            txs.append(tx)
            line = line.split()
            if line[0].lower() == 'transfer':
                tx.type = TransferAssetsTransaction
                tx.wallet_to = line[2]
                if line[1].lower() == 'xdr':
                    tx.xdr = Decimal(line[3])
                elif line[1].lower() == 'mile':
                    tx.mile = Decimal(line[3])
                else:
                    assert False
            elif line[0].lower() == 'amount-register-node':
                tx.type = RegisterNodeTransactionWithAmount
                tx.wallet_from = line[1]
                tx.node_address = line[2]
                tx.xdr = Decimal(line[3])
            else:
                assert False

    block.transactions_count = len(txs)

    async with db.transaction():
        await block.create()
        for tx in txs:
            await tx.create()
        await _make_wallets_dirty(txs, block.id)

    await _process_txs_wallets(fetch_tasks, txs)


async def _process_wallet(pub_key):
    wallet = await Wallet.get(pub_key)
    if not wallet:
        await db.status(
            """
                insert into wallets (pub_key, created_at, valid_before_block) 
                values ($1, now(), null) on conflict (pub_key) do nothing 
            """,
            pub_key
        )
        wallet = await Wallet.get(pub_key)

    block_id = wallet.valid_before_block
    if block_id is not None:
        data = await get_wallet_after_block(pub_key, block_id)
    else:
        logger.warning(f"Processing wallet without block_id check: {pub_key}. Update created_at")
        data = await get_wallet(pub_key)

    wallet.valid_before_block = None
    wallet.balance_updated_at = datetime.utcnow()

    if 'Node' in data.get('tags', []):
        wallet.is_node = True
    wallet.node_address = data.get('address')

    for balance in data['balance']:
        assert 'frozen' in balance or 'freezed' in balance
        if balance['code'] == '0':
            wallet.xdr_balance = Decimal(balance['amount'])
            wallet.xdr_staked = Decimal(balance.get('frozen', balance.get('freezed')))
        elif balance['code'] == '1':
            wallet.mile_balance = Decimal(balance['amount'])
            wallet.mile_staked = Decimal(balance.get('frozen', balance.get('freezed')))
        else:
            logger.error(f"Unknown code: {balance['code']}")
            return True  # for manual processing later

    await wallet.update(**wallet.to_dict()).apply()
    return True


async def _process_block(block_id, fetch_tasks: deque):
    data = await get_block(block_id)

    block = Block(
        id=int(data["id"]),
        version=int(data["version"]),
        previous_block_digest=data["previous-block-digest"],
        merkle_root=data["merkle-root"],
        timestamp=parse(data["timestamp"]),
        transactions_count=int(data["transaction-count"]),
        number_of_signers=int(data["number-of-signers"]),
        round=int(data["round"]),
        block_header_digest=data["block-header-digest"],
        main_signer=data["escort-signatures"][0]['key']
    )

    txs = []
    for i, tx_data in enumerate(list(data['fee-transactions'])):
        tx = Transaction(
            digest=f"{tx_data['digest']}_{block_id}_{i}",
            block_id=block.id,
            num_in_block=-len(list(data['fee-transactions']))+i,
            timestamp=block.timestamp,
            global_num=int(tx_data.get('transaction-id', 0)),
            is_fee=True,
            type=TX_TYPES[tx_data['transaction-type']],
            fee=Decimal(tx_data.get('fee', 0)),
            signature=tx_data['signature'],
            description=tx_data.get('description', '')
        )

        if not _fill_tx_type_specific_data(tx, tx_data, block):
            block.reindex_needed = True

        if tx.type == TransferAssetsTransaction:
            if tx.xdr or tx.mile:
                txs.append(tx)
        else:
            logger.warning(f"Unknown type in fee txs: {tx.type}")

        tx.wallet_from = None

    for num, tx_data in enumerate(list(data['transactions'])):
        tx = Transaction(
            digest=tx_data['digest'],
            block_id=block.id,
            num_in_block=num,
            timestamp=block.timestamp,
            global_num=int(tx_data.get('transaction-id', 0)),
            is_fee=False,
            type=TX_TYPES[tx_data['transaction-type']],
            fee=Decimal(tx_data.get('fee', 0)),
            signature=tx_data['signature'],
            description=tx_data.get('description', '')
        )
        txs.append(tx)

        if not _fill_tx_type_specific_data(tx, tx_data, block):
            block.reindex_needed = True

    async with db.transaction():
        # r = await Wallet.query.gino.all()
        old_block: Block = await Block.get(block.id)
        if old_block and old_block.reindex_needed:
            await Block.delete.where(Block.id==block.id).gino.status()
            await Transaction.delete.where(Transaction.block_id == block.id).gino.status()
        elif old_block:
            logger.error(f"Task to index, but reindex_needed=False: {block.id}")
            return True

        await block.create()
        for tx in txs:
            await tx.create()
        await _make_wallets_dirty(txs, block.id)

    await _process_txs_wallets(fetch_tasks, txs)

    return True


async def _make_wallets_dirty(txs, block_id):
    wallets = []
    for tx in txs:
        for wallet in (tx.wallet_from, tx.wallet_to):
            if wallet:
                wallets.append(wallet)

    if wallets:
        await Wallet.update.values(valid_before_block=block_id).where(
            Wallet.pub_key.in_(wallets)).gino.status()


async def _process_txs_wallets(fetch_tasks: deque, txs=None, pub_keys_with_ts_blockid=None):
    assert txs is not None or pub_keys_with_ts_blockid is not None

    if pub_keys_with_ts_blockid is None:
        pub_keys_with_ts_blockid = []
    if txs is not None:
        for tx in txs:
            for wallet in (tx.wallet_from, tx.wallet_to):
                if wallet:
                    pub_keys_with_ts_blockid.append((wallet, tx.timestamp, tx.block_id))

    for wallet, ts, block_id in pub_keys_with_ts_blockid:
        await db.status(
            """
                insert into wallets (pub_key, created_at, valid_before_block) 
                values ($1, $2, $3) on conflict (pub_key) do update set valid_before_block=$4
            """,
            wallet,
            ts,
            block_id, block_id
        )
        fetch_tasks.append( (TASK_WALLET, wallet, datetime.now()) )
        fetch_tasks.append( (TASK_WALLET, wallet, datetime.now()+timedelta(minutes=10)) )


def _fill_tx_type_specific_data(tx, tx_data, block):
    tx_type = tx_data['transaction-type']

    if tx_type == 'TransferAssetsTransaction':
        tx.wallet_from = tx_data['from']
        tx.wallet_to = tx_data['to']

        if len(tx_data['asset']) > 1:
            logger.warning(f"Several assets in one transfer: {block.id}")
            return False

        for asset_data in list(tx_data['asset']):
            if asset_data['code'] == "0":
                tx.xdr = Decimal(asset_data['amount'])
            elif asset_data['code'] == "1":
                tx.mile = Decimal(asset_data['amount'])
            else:
                logger.warning(f"Unknown asset: {block.id}, {asset_data['code']}")
                return False

    elif tx_type == 'EmissionTransaction':
        tx.wallet_from = tx_data['from']
        tx.asset = int(tx_data['code'])

    elif tx_type == 'RegisterNodeTransactionWithAmount':
        tx.wallet_from = tx_data['public-key']
        tx.node_address = tx_data['address']
        tx.xdr = Decimal(tx_data['amount'])

    elif tx_type == 'UnregisterNodeTransaction':
        tx.wallet_from = tx_data['public-key']

    elif tx_type == 'PostTokenRate':
        tx.wallet_from = tx_data['public-key']
        tx.rate = Decimal(tx_data['course'])

    elif tx_type == 'CreateTokenRateVoting':
        tx.wallet_from = tx_data['public-key']

    elif tx_type == 'GetTokenRate':
        tx.wallet_from = tx_data['public-key']
        tx.rate = Decimal(tx_data['course'])

    elif tx_type == 'UpdateEmission':
        tx.wallet_from = tx_data['from']

    else:
        logger.warning(f"Unknown tx type: {block.id}, {tx_type}")
        return False

    return True
