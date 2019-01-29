import asyncio
from copy import deepcopy
from functools import partial
from operator import itemgetter

import aiohttp
from bs4 import BeautifulSoup
import cchardet
import formasaurus
import logging
import trio
import trio_asyncio
import w3lib.encoding
from yarl import URL

from .downloader import DownloadRequest
from .policy import PolicyMimeTypeRules


logger = logging.getLogger(__name__)
chardet = lambda s: cchardet.detect(s).get('encoding')


async def get_login_form(policy, downloader, cookie_jar, response,
        username, password):
    '''
    Attempt to extract login form action and form data from a response,
    substituting the provided ``username`` and ``password`` into the
    corresponding fields. Returns the data needed to POST a login request.

    :param starbelly.policy.Policy: The policy to use when downloading the
        login form.
    :param starbelly.downloader.Downloader: The downloader to use for the login
        form and CAPTCHA.
    :param cookie_jar: An aiohttp cookie jar.
    :param starbelly.downloader.DownloadResponse response:
    :param str username: The username to log in with.
    :param str password: The password to log in with.
    :returns: (action, method, fields)
    :rtype: tuple
    '''
    encoding, html = w3lib.encoding.html_to_unicode(
        response.content_type,
        response.body,
        auto_detect_fun=chardet
    )

    forms = await trio.run_sync_in_worker_thread(partial(
        formasaurus.extract_forms, html, proba=True))
    form, meta = _select_login_form(forms)

    if form is None:
        raise Exception("Can't find login form")

    login_field, password_field, captcha_field = _select_login_fields(
        meta['fields'])
    if login_field is None or password_field is None:
        raise Exception("Can't find username/password fields")

    form.fields[login_field] = username
    form.fields[password_field] = password

    if captcha_field is not None:
        if policy.captcha_solver is None:
            raise Exception('CAPTCHA required for login url={} but there is'
                ' no CAPTCHA solver available'.format(response.url))

        img_el = _get_captcha_image_element(form)
        img_src = str(URL(response.url).join(URL(img_el.get('src'))))
        img_data = await _download_captcha_image(policy, downloader, cookie_jar,
            img_src)
        captcha_text = await solve_captcha_asyncio(policy.captcha_solver,
            img_data)
        form.fields[captcha_field] = captcha_text

    form_action = URL(response.url).join(URL(form.action))
    return form_action, form.method, dict(form.fields)


@trio_asyncio.aio_as_trio
async def solve_captcha_asyncio(solver, img_data):
    '''
    Send an image CAPTCHA to an external solver and return the solution.
    This function uses aiohttp and therefore must run on the asyncio loop.

    :param bytes img_data: The CAPTCHA image.
    :rtype: str
    '''
    solution = None
    task_url = str(URL(solver.service_url).join(URL('createTask')))
    poll_url = str(URL(solver.service_url).join(URL('getTaskResult')))

    # This doesn't use the downloader object because this is a third party
    # and is not the subject of our crawl.
    async with aiohttp.ClientSession() as session:
        # Send CAPTCHA task to service
        command = solver.get_command(img_data)
        async with session.post(task_url, json=command) as response:
            result = await response.json()
            if result['errorId'] != 0:
                raise Exception('CAPTCHA API error {}'
                    .format(result['errorId']))
            task_id = result['taskId']
            logger.info('Sent image to CAPTCHA API task_id=%d', task_id)

        # Poll for task completion. (Try 6 times.)
        solution = None
        for attempt in range(6):
            await asyncio.sleep(5)
            command = {
                'clientKey': solver.api_key,
                'taskId': task_id,
            }
            logger.info('Polling for CAPTCHA solution task_id=%d,'
                ' attempt=%d', task_id, attempt+1)
            async with session.post(poll_url, json=command) as response:
                result = await response.json()
                if result['errorId'] != 0:
                    raise Exception('CAPTCHA API error {}'
                        .format(result['errorId']))
                elif result['status'] == 'ready':
                    solution = result['solution']['text']
                    break

    if solution is None:
        raise Exception('CAPTCHA API never completed task')

    return solution


async def _download_captcha_image(policy, downloader, cookie_jar, img_src):
    '''
    Download and return a CAPTCHA image.

    :param starbelly.policy.Policy: The policy to use when downloading the
        CAPTCHA image.
    :param starbelly.downloader.Downloader: The downloader to use for the login
        form and CAPTCHA.
    :param cookie_jar: An aiohttp cookie jar.
    :param str img_src: The URL to download the image from.
    :rtype bytes:
    '''
    logger.info('Downloading CAPTCHA image src=%s', img_src)
    request = DownloadRequest(
        job_id=None,
        method='GET',
        url=img_src,
        form_data=None,
        cost=0,
        policy=policy,
        cookie_jar=cookie_jar,
    )
    response = await downloader.download(request)

    if response.status_code == 200 and response.body is not None:
        img_data = response.body
    else:
        raise Exception('Failed to download CAPTCHA image src=%s', img_src)

    return img_data


def _get_captcha_image_element(form):
    '''
    Return the <img> element in an lxml form that contains the CAPTCHA.

    NOTE: This assumes the first image in the form is the CAPTCHA image. If
    a form has multiple images, maybe use the etree .sourceline attribute to
    figure out which image is closer to the CAPTCHA input? Or crawl through
    the element tree to find the image?

    :param form: An lxml form element.
    :returns: An lxml image element.
    '''
    img_el = form.find('img')
    if img_el is None:
        raise Exception('Cannot locate CAPTCHA image')
    return img_el


def _select_login_fields(fields):
    '''
    Select field having highest probability for class ``field``.

    :param dict fields: Nested dictionary containing label probabilities
        for each form element.
    :returns: (username field, password field, captcha field)
    :rtype: tuple
    '''
    username_field = None
    username_prob = 0
    password_field = None
    password_prob = 0
    captcha_field = None
    captcha_prob = 0

    for field_name, labels in fields.items():
        for label, prob in labels.items():
            if (label == 'username' or label == 'username or email') \
                and prob > username_prob:
                username_field = field_name
                username_prob = prob
            elif label == 'password' and prob > password_prob:
                password_field = field_name
                password_prob = prob
            elif label == 'captcha' and prob > captcha_prob:
                captcha_field = field_name
                captcha_prob = prob

    return username_field, password_field, captcha_field


def _select_login_form(forms):
    '''
    Select form having highest probability for login class.

    :param dict forms: Nested dict containing label probabilities for each
        form.
    :returns: (login form, login meta)
    :rtype: tuple
    '''
    login_form = None
    login_meta = None
    login_prob = 0

    for form, meta in forms:
        for type_, prob in meta['form'].items():
            if type_ == 'login' and prob > login_prob:
                login_form = form
                login_meta = meta
                login_prob = prob

    return login_form, login_meta
