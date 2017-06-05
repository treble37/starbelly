'''
A very minimal client that only demonstrates some of the basic features of the
Starbelly API. This is not intended for production use.

This script uses asyncio only because starbelly already has the async websockets
library as a dependency and I didn't want to add a dependency on a synchronous
websockets library just for this sample client. This isn't a good example of
async programming!
'''

import argparse
import asyncio
import binascii
import gzip
import logging
import ssl
import sys
import termios
import textwrap
import tty
from uuid import UUID

import dateutil.parser
import websockets
import websockets.exceptions

import protobuf.client_pb2
import protobuf.shared_pb2
import protobuf.server_pb2


logging.basicConfig()
logger = logging.getLogger('sample_client')
DATE_FMT = '%Y-%m-%d %H:%I:%S'


async def start_crawl(args, socket):
    ''' Start a new crawl. '''
    request = protobuf.client_pb2.Request()
    request.request_id = 1
    request.start_job.policy_id = UUID(args.policy).bytes
    if args.name:
        request.start_job.name = args.name
    for seed in args.seed:
        request.start_job.seeds.append(seed)
    logger.error('request=%r', request)
    request_data = request.SerializeToString()
    await socket.send(request_data)

    message_data = await socket.recv()
    message = protobuf.server_pb2.ServerMessage.FromString(message_data)
    if message.response.is_success:
        job_id = binascii.hexlify(message.response.new_job.job_id)
        print('Started job: {}'.format(job_id.decode('ascii')))
    else:
        print('Failed to start job: {}'.format(message.response.error_message))


async def delete_job(args, socket):
    ''' Delete a job. '''
    request = protobuf.client_pb2.Request()
    request.request_id = 1
    request.delete_job.job_id = UUID(args.job_id).bytes
    request_data = request.SerializeToString()
    await socket.send(request_data)

    message_data = await socket.recv()
    message = protobuf.server_pb2.ServerMessage.FromString(message_data)
    if message.response.is_success:
        print('Job deleted.')
    else:
        print('Failed to delete job: {}'.format(message.response.error_message))


def get_args():
    ''' Parse command line arguments. '''
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        '-v',
        dest='verbosity',
        default='warning',
        choices=['debug', 'info', 'warning', 'error', 'critical'],
        help='Set logging verbosity. Defaults to "warning".'
    )

    parser.add_argument(
        'host',
        help='The name or IP of the starbelly host.'
    )

    subparsers = parser.add_subparsers(help='Actions', dest='action')
    subparsers.required = True
    crawl_parser = subparsers.add_parser('crawl', help='Start a crawl.')
    crawl_parser.add_argument('-n', '--name',
        help='Assign a name to this crawl.')
    crawl_parser.add_argument('policy',
        help='A policy ID.')
    crawl_parser.add_argument('seed',
        nargs='+',
        help='One or more seeds.')
    list_parser = subparsers.add_parser('list', help='List crawl jobs.')
    show_parser = subparsers.add_parser('show', help='Display a crawl job.')
    show_parser.add_argument('job_id', help='Job ID as hex string.')
    show_parser.add_argument('--items', action='store_true',
        help='Show some of the job\'s items.')
    show_parser.add_argument('--errors', action='store_true',
        help='Show some of the job\'s HTTP errors.')
    show_parser.add_argument('--exceptions', action='store_true',
        help='Show some of the job\'s exceptions.')
    delete_parser = subparsers.add_parser('delete', help='Delete a crawl job.')
    delete_parser.add_argument('job_id', help='Job ID as hex string.')
    sync_parser = subparsers.add_parser('sync',
        help='Sync items from a job.')
    sync_parser.add_argument('job_id', help='Job ID as hex string.')
    sync_parser.add_argument('-d', '--delay', type=float, default=0,
        help='Delay between printing items (default 0).')
    sync_parser.add_argument('-t', '--token',
        help='To resume syncing, supply a sync token.')
    rate_limit_parser = subparsers.add_parser('rates',
        help='Show rate limits.')
    rate_limit_parser = subparsers.add_parser('set_rate',
        help='Set a rate limit.')
    rate_limit_parser.add_argument('delay', type=float,
        help='Delay in seconds. (-1 to clear)')
    rate_limit_parser.add_argument('domain',
        nargs='?',
        help='Domain name to rate limit. (If omitted, modifies global limit.)')

    args = parser.parse_args()
    logger.setLevel(getattr(logging, args.verbosity.upper()))
    return args


def getch():
    '''
    Thanks, stackoverflow.
    http://stackoverflow.com/questions/510357/python-read-a-single-character-from-the-user
    '''
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch


async def list_jobs(args, socket):
    ''' List crawl jobs on the server. '''
    current_page = 0
    action = 'm'

    print('| {:20s} | {:32s} | {:9s} | {:19s} | {:5s} |'
        .format('Name', 'ID', 'Status', 'Started', 'Items'))
    print('-' * 101)

    while action == 'm':
        current_page += 1
        limit = 10
        offset = (current_page - 1) * limit

        request = protobuf.client_pb2.Request()
        request.request_id = 1
        request.list_jobs.page.limit = limit
        request.list_jobs.page.offset = offset
        request_data = request.SerializeToString()
        await socket.send(request_data)

        message_data = await socket.recv()
        message = protobuf.server_pb2.ServerMessage.FromString(message_data)
        response = message.response
        for job in response.list_jobs.jobs:
            run_state = protobuf.shared_pb2.JobRunState.Name(job.run_state)
            print('| {:20s} | {:32s} | {:9s} | {:19s} | {:5d} |'.format(
                job.name[:20],
                binascii.hexlify(job.job_id).decode('ascii'),
                run_state,
                job.started_at[:19],
                job.item_count
            ))
        start = offset + 1
        end = offset + len(response.list_jobs.jobs)
        total = response.list_jobs.total
        if end == total:
            print('Showing {}-{} of {}.'.format(start, end, total))
            action = 'q'
        else:
            print('Showing {}-{} of {}. [m]ore or [q]uit?'
                .format(start, end, total))
            action = await asyncio.get_event_loop().run_in_executor(None, getch)


async def main():
    ''' Main entry point. '''
    args = get_args()

    actions = {
        'crawl': start_crawl,
        'delete': delete_job,
        'list': list_jobs,
        'rates': get_rates,
        'set_rate': set_rate,
        'show': show_job,
        'sync': sync_job,
    }

    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    url = 'wss://{}/ws/'.format(args.host)
    logger.info('Connecting to %s', url)

    socket = await websockets.connect(url, ssl=ssl_context)
    await actions[args.action](args, socket)

    if socket.open:
        await socket.close()


async def get_rates(args, socket):
    ''' Show rate limits. '''
    current_page = 0
    action = 'm'

    print('| {:40s} | {:5} |'.format('Name', 'Delay'))
    print('-' * 52)

    while action == 'm':
        current_page += 1
        limit = 10
        offset = (current_page - 1) * limit

        request = protobuf.client_pb2.Request()
        request.request_id = 1
        request.get_rate_limits.page.limit = limit
        request.get_rate_limits.page.offset = offset
        request_data = request.SerializeToString()
        await socket.send(request_data)

        message_data = await socket.recv()
        message = protobuf.server_pb2.ServerMessage.FromString(message_data)
        response = message.response
        for rate_limit in response.list_rate_limits.rate_limits:
            name = rate_limit.name
            print('| {:40s} | {:5.3f} |'.format(
                rate_limit.name[:40],
                rate_limit.delay
            ))
        start = offset + 1
        end = offset + len(response.list_rate_limits.rate_limits)
        total = response.list_rate_limits.total
        if end == total:
            print('Showing {}-{} of {}.'.format(start, end, total))
            action = 'q'
        else:
            print('Showing {}-{} of {}. [m]ore or [q]uit?'
                .format(start, end, total))
            action = await asyncio.get_event_loop().run_in_executor(None, getch)    #


async def set_rate(args, socket):
    ''' Set a rate limit. '''
    request = protobuf.client_pb2.Request()
    request.request_id = 1
    rate_limit = request.set_rate_limit.rate_limit
    if args.domain is not None:
        rate_limit.domain = args.domain
    if args.delay >= 0:
        rate_limit.delay = args.delay
    if not (rate_limit.HasField('domain') or rate_limit.HasField('delay')):
        logger.error('Delay must be >= 0 if domain not specified.')
        return
    request_data = request.SerializeToString()
    await socket.send(request_data)

    message_data = await socket.recv()
    message = protobuf.server_pb2.ServerMessage.FromString(message_data)
    if message.response.is_success:
        domain = '(global)' if args.domain is None else args.domain
        delay = args.delay if args.delay >= 0 else '(deleted)'
        print('Set rate limit: {}={}'.format(domain, delay))
    else:
        print('Failed to set rate limit: {}'
            .format(message.response.error_message))


async def show_job(args, socket):
    ''' Show a single job. '''
    request = protobuf.client_pb2.Request()
    request.request_id = 1
    request.get_job.job_id = UUID(args.job_id).bytes
    request_data = request.SerializeToString()
    await socket.send(request_data)

    message_data = await socket.recv()
    message = protobuf.server_pb2.ServerMessage.FromString(message_data)
    job = message.response.job
    run_state = protobuf.shared_pb2.JobRunState.Name(job.run_state)
    started_at = dateutil.parser.parse(job.started_at).strftime(DATE_FMT)
    if job.HasField('completed_at'):
        completed_at = dateutil.parser.parse(job.completed_at).strftime(DATE_FMT)
    else:
        completed_at = 'N/A'
    print('ID:           {}'.format(UUID(bytes=job.job_id)))
    print('Name:         {}'.format(job.name))
    print('Run State:    {}'.format(run_state))
    print('Started At:   {}'.format(started_at))
    print('Completed At: {}'.format(completed_at))
    print('Items Count:  success={}, error={}, exception={} (total={})'.format(
        job.http_success_count, job.http_error_count, job.exception_count,
        job.item_count
    ))

    print('Seeds:')
    for seed in job.seeds:
        print(' * {}'.format(seed))

    if len(job.http_status_counts) > 0:
        print('HTTP Status Codes:')
        for code, count in job.http_status_counts.items():
            print(' * {:d}: {:d}'.format(code, count))

    if args.items or args.errors or args.exceptions:
        request = protobuf.client_pb2.Request()
        request.request_id = 1
        request.get_job_items.job_id = UUID(args.job_id).bytes
        request.get_job_items.include_success = args.items
        request.get_job_items.include_error = args.errors
        request.get_job_items.include_exception = args.exceptions
        request_data = request.SerializeToString()
        await socket.send(request_data)

        message_data = await socket.recv()
        message = protobuf.server_pb2.ServerMessage.FromString(message_data)
        items = message.response.list_items.items
        total = message.response.list_items.total

        if len(items) == 0:
            print('No items matching the requested flags'
                ' (success={} errors={} exceptions={})'
                .format(args.items, args.errors, args.exceptions))
        else:
            print('\nShowing {} of {} matching items (success={} errors={}'
                ' exceptions={})'.format(len(items), total, args.items,
                args.errors, args.exceptions))
            for item in items:
                started_at = dateutil.parser.parse(item.started_at) \
                    .strftime(DATE_FMT)
                completed_at = dateutil.parser.parse(item.completed_at) \
                    .strftime(DATE_FMT)
                if item.HasField('body'):
                    if item.is_body_compressed:
                        body = gzip.decompress(item.body)
                    else:
                        body = item.body
                else:
                    body = None
                print('\n' + '=' * 60)
                print('{}'.format(item.url))
                print('Status: {}\nCost: {}\nContent-Type: {}'.format(
                    item.status_code, item.cost, item.content_type))
                print('Started: {}\nCompleted: {}\nDuration: {}s '.format(
                    started_at, completed_at, item.duration))
                if body is not None:
                    print('Body: {}'.format(repr(body)))
                if item.HasField('exception'):
                    print('Exception: \n{}'.format(
                        textwrap.indent(item.exception, prefix='> ')))


async def sync_job(args, socket):
    ''' Sync items from a job. '''

    request = protobuf.client_pb2.Request()
    request.request_id = 1
    request.subscribe_job_sync.job_id = binascii.unhexlify(args.job_id)
    if args.token is not None:
        request.subscribe_job_sync.sync_token = binascii.unhexlify(args.token)
    request_data = request.SerializeToString()
    await socket.send(request_data)

    message_data = await socket.recv()
    message = protobuf.server_pb2.ServerMessage.FromString(message_data)
    response = message.response
    if not response.is_success:
        raise Exception('Server failure: ' + response.error_message)

    print('| {:50s} | {:5s} | {:10s} |'.format('URL', 'Cost', 'Size (KB)'))
    print('-' * 75)
    sync_token = None

    try:
        while True:
            message_data = await socket.recv()
            message = protobuf.server_pb2.ServerMessage.FromString(message_data)
            event_type = message.event.WhichOneof('Body')
            if event_type == 'subscription_closed':
                print('-- End of crawl results ---')
                break
            item = message.event.sync_item.item
            sync_token = message.event.sync_item.token
            print('| {:50s} | {:5.1f} | {:10.2f} |'.format(
                item.url[:50],
                item.cost,
                len(item.body) / 1024
            ))
            await asyncio.sleep(args.delay)
    except asyncio.CancelledError:
        print('Interrupted! To resume sync, use token: {}'
            .format(binascii.hexlify(sync_token).decode('ascii')))


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    main_task = asyncio.ensure_future(main())
    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        main_task.cancel()
        loop.run_until_complete(main_task)
    except websockets.exceptions.ConnectionClosed:
        logger.error('Server unexpectedly closed the connection.')
    loop.close()
