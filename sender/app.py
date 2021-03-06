import json
import os
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pika
import requests
import validators

from AssetMetaData import Asset
from MongodbConnection import MongodbConnection
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, request

from consts import PRICE_CLASS, HEADERS, URL_PREFIX, BUTTON_TYPE_CLASS, FLOOR_PRICE_CLASS, SCRAPE_MODE_ASSETS, \
    SCRAPE_MODE_COLLECTIONS
from flask_cors import cross_origin, CORS

load_dotenv()

app = Flask(__name__)

# cors = CORS(app, resources={r"/*": {"origins": ["http://localhost:3000",
#                                                 "http://yuvalta.github.io/*",
#                                                 "https://yuvalta.github.io/*",
#                                                 "http://yuvalta.github.io",
#                                                 "https://yuvalta.github.io"]}})

CORS(app)

app.config['CORS_HEADERS'] = 'Content-Type'

loop_flag = True


# when user adding new asset from UI
def validate_input_url(string_to_validate):
    if len(str(string_to_validate)) == 0:
        return False
    if not validators.url(string_to_validate):
        return False

    return True


def validate_input_email(string_to_validate):
    if len(str(string_to_validate)) == 0:
        return False
    if not validators.email(string_to_validate):
        return False

    return True


@app.route('/upsert_asset/', methods=['POST'])
def upsert_asset():
    # TODO: validate args
    asset_url = request.json["contract_id"]
    user_email = request.json["user_email"]
    mode = request.json["mode"]

    # if not validate_input_url(asset_url):
    #     return {"error": "error in input"}
    # if not validate_input_email(user_email):
    #     return {"error": "error in input"}

    app.logger.info("upsert_asset() %s %s", asset_url, user_email)

    return add_user_to_asset(asset_url, user_email, mode)


# start loop
@app.route('/start/', methods=['GET'])
def start():
    app.logger.info("start looping")
    global loop_flag
    app.logger.info(loop_flag)

    while loop_flag:
        try:
            # scrape floor price
            collection_col = MongodbConnection.get_instance()["CollectionsCol"]
            # cursor from mongodb
            full_collections_list = collection_col.find({})

            mapped_floor_list, bulk_contracts_list = create_mapped_assets_list(full_collections_list)
            app.logger.info("bulk_contracts_list")
            app.logger.info(bulk_contracts_list)

            response = get_bulk_floor_price_api(bulk_contracts_list)
            if response["response"] != 200:
                app.logger.info("error in api call")
                time.sleep(30)
                continue

            app.logger.info("scraping " + str(len(mapped_floor_list)) + " collections floor prices")

            # dict with contract id as Key and floor price as Value
            # this dict is helping me to compare price in more efficient way
            updated_price_chart = create_response_dict(response)

            compare_floor_price_with_chart(updated_price_chart, mapped_floor_list)

            app.logger.info("finished loop, sleeping...")
            time.sleep(90)

        except Exception as e:
            app.logger.info("Exception in loop " + str(e))
            time.sleep(90)

    return "Finished loop!"


def compare_floor_price_with_chart(updated_price_chart, mapped_floor_list):
    for asset_db in mapped_floor_list:
        try:
            app.logger.info(asset_db.to_json())
            asset_db.action = SCRAPE_MODE_COLLECTIONS
            if asset_db.price != updated_price_chart[str(asset_db.contract_id).lower()]:
                app.logger.info("in compare_floor_price_response - need to notify")
                asset_db.need_to_notify = True
                asset_db.price = updated_price_chart[asset_db.contract_id]
            else:
                app.logger.info("in compare_floor_price_response - no need to notify")
                asset_db.need_to_notify = False

        except Exception as e:
            app.logger.error("Except in scrape " + str(e) + " contract - " + asset_db.contract_id)
            asset_db.error_message = str(e)
        finally:
            if asset_db.need_to_notify:
                update_asset_in_asset_col_db(asset_db)
                # push_to_queue(asset_db)


def create_response_dict(response):
    price_chart = {}
    for contract in response["data"]:
        contract_id = contract["asset_contract"]
        new_floor_price = contract["floor_price"][1]["floor_price"]

        price_chart[str(contract_id).lower()] = new_floor_price

    app.logger.info("create_response_dict")
    app.logger.info(price_chart)
    return price_chart


def get_bulk_floor_price_api(bulk_contracts_list):
    headers = {
        'x-api-key': str(os.environ.get('NFTBANK_API_KEY')),
    }

    json_data = {
        'chain_id': 'ETHEREUM',
        'asset_contracts':
            bulk_contracts_list,
    }

    response = requests.post('https://api.nftbank.ai/estimates-v2/floor_price/bulk', headers=headers, json=json_data)
    app.logger.info(response.json()["response"])

    return response.json()


# stop loop
@app.route('/stop/', methods=['GET'])
def stop():
    app.logger.info("stop looping")

    global loop_flag
    loop_flag = False

    return "Stopped!"


@app.route('/get_assets_for_user/', methods=['POST'])
def get_assets_for_user():
    app.logger.info("get_assets_for_user")
    user_email = request.json["user_email"]
    mode = request.json["mode"]
    app.logger.info(user_email)

    if mode == SCRAPE_MODE_COLLECTIONS:
        col = MongodbConnection.get_instance()["CollectionsCol"]
    else:
        col = MongodbConnection.get_instance()["AssetsCol"]
    asset_query = {"users": user_email}

    cursor = col.find(asset_query)
    assets_list = []
    for asset in cursor:
        assets_list.append({"contract_id": asset["contract_id"], "price": asset["price"], "action": asset["action"]})

    return json.dumps(assets_list)


@app.route('/delete_all_from_assets_col/')
def delete_all_from_assets_col():
    col = MongodbConnection.get_instance()["AssetsCol"]
    col.delete_many({})
    return {"delete_all": "deleted"}


@app.route('/delete_user_from_asset/', methods=['POST'])
def delete_user_from_asset():
    user_email = request.json["user_email"]
    asset_url = request.json["contract_id"]
    app.logger.info("delete_user_from_asset: " + user_email + " " + asset_url)

    error_message = ""

    col = MongodbConnection.get_instance()["AssetsCol"]

    asset_query = {"contract_id": asset_url}
    retrieved_asset_from_db = col.find_one(asset_query)

    user_list = set(retrieved_asset_from_db["users"])

    if len(user_list) == 1 and user_email in user_list:
        # only this user in asset, delete asset
        try:
            col.delete_one(asset_query)
        except Exception as e:
            error_message = str(e)
        return {"response": "Asset deleted!", "error": error_message}

    if len(user_list) > 0:
        user_list.remove(user_email)

    new_values = {"$set": {"users": list(user_list)}}
    try:
        col.update_one(asset_query, new_values)
    except Exception as e:
        error_message = str(e)

    return {"response": "User deleted from asset!", "error": error_message}


# mapping db objects to AssetMetaData object and append to truncated list
def create_mapped_assets_list(full_assets_list):
    mapped_assets_list = []
    bulk_contracts_list = []
    try:
        for asset in full_assets_list:
            mapped_assets_list.append(
                Asset(asset["contract_id"], asset["users"], asset["price"], asset["error_message"],
                      asset["need_to_notify"], asset["action"]))
            bulk_contracts_list.append(asset["contract_id"])

    except Exception as e:
        app.logger.info("Exception in create_mapped_assets_list - " + str(e))
    return mapped_assets_list, bulk_contracts_list


# check if asset url in db. if so, add user to this asset. if not, add new asset to db
def add_user_to_asset(asset_url, user, mode):
    try:
        if mode == SCRAPE_MODE_COLLECTIONS:
            col = MongodbConnection.get_instance()["CollectionsCol"]
        else:
            col = MongodbConnection.get_instance()["AssetsCol"]

        app.logger.info("add_user_to_asset - " + mode)

        asset_query = {"contract_id": asset_url}
        retrieved_asset_from_db = col.find_one(asset_query)

        app.logger.info(retrieved_asset_from_db)

        if retrieved_asset_from_db is None:
            app.logger.info("adding new " + mode)
            new_asset_user_list = [user]
            add_new_asset(col, Asset(asset_url, new_asset_user_list, "new asset", "", False, ""))
            return {"response": "added new " + mode}
        else:
            app.logger.info("updating existing " + mode)
            new_user_list = set(retrieved_asset_from_db["users"])

            if len(new_user_list) > 20:
                error_msg = "Too many users on this url"
                app.logger.info(error_msg)
                return {"response": "", "error": error_msg}

            if user in new_user_list:
                return {"response": "", "error": "User already Exist in " + mode}

            new_user_list.add(user)

            new_values = {"$set": {"users": list(new_user_list)}}
            col.update_one(asset_query, new_values)
            return {"response": "updating existing " + mode}

    except Exception as e:
        app.logger.info(str(e))
        return str(e)


def add_new_asset(col, new_asset):
    app.logger.info("add_new_asset: " + new_asset.to_json())
    col.insert_one(new_asset.__dict__)


def update_asset_in_asset_col_db(asset_to_queue):
    try:
        app.logger.info("Updating asset: " + asset_to_queue.contract_id + " to price: "
                        + asset_to_queue.price + " Action: " + asset_to_queue.action)
    except:
        app.logger.info("update_asset_in_asset_col_db")

    asset_query = {"contract_id": asset_to_queue.contract_id}
    new_values = {"$set": {"price": asset_to_queue.price, "action": asset_to_queue.action,
                           "need_to_notify": asset_to_queue.need_to_notify}}

    if asset_to_queue.action == SCRAPE_MODE_COLLECTIONS:
        col = MongodbConnection.get_instance()["CollectionsCol"]
    else:
        col = MongodbConnection.get_instance()["AssetsCol"]

    col.update_one(asset_query, new_values)


def get_page_content_collection(full_url):
    try:
        req = urllib.request.Request(url=full_url, headers=HEADERS)
        page = urllib.request.urlopen(req).read()

        soup = BeautifulSoup(page, features="html.parser")

        try:
            content_floor_price = soup.find_all("div", class_=FLOOR_PRICE_CLASS)[2]
        except Exception as e:
            app.logger.error("Except in content_floor_price" + str(e))
            return None

        return content_floor_price

    except Exception as e:  # general exception
        app.logger.error("Except in get_page_content_collection " + str(e))
        return None


def scrape_asset_data(assets_to_queue, scraping_mode):
    for asset_to_queue in assets_to_queue:
        is_new_asset = (asset_to_queue.price == "new asset")
        try:
            full_url = asset_to_queue.contract_id
            if scraping_mode == SCRAPE_MODE_ASSETS:
                content_price, content_button = get_page_content(full_url)
                # if getting 429 error, try again 3 times
                if content_price == 429:
                    abort_retry = False
                    for tries in range(3):
                        time.sleep(10)
                        content_price, content_button = get_page_content(full_url)
                        # abort on 3rd try
                        if tries == 2:
                            abort_retry = True

                    if abort_retry:
                        continue
            else:
                content_price = get_page_content_collection(full_url)
                content_button = SCRAPE_MODE_COLLECTIONS

            # no price found
            if content_price is None:
                # same as last iteration
                if asset_to_queue.price == "No price!":
                    continue

                # price changed to No price, need to update + notify
                asset_to_queue.need_to_notify = True
                asset_to_queue.price = "No price!"
                asset_to_queue.action = content_button

            # price found
            else:
                new_price = content_price.contents[0]

                # same price
                if new_price == asset_to_queue.price:
                    continue

                # new price, need to update + notify
                asset_to_queue.need_to_notify = True
                asset_to_queue.action = content_button
                asset_to_queue.price = new_price

        except Exception as e:
            app.logger.error("Except in scrape " + scraping_mode + " - " + str(e))
            asset_to_queue.error_message = str(e)

        finally:
            if asset_to_queue.need_to_notify:
                update_asset_in_asset_col_db(asset_to_queue)
                if not is_new_asset:
                    push_to_queue(asset_to_queue)


def detect_action(action):
    if str(action) == "account_balance_wallet":
        return "Buy Now"
    if str(action) == "local_offer":
        return "Bid"
    if str(action) == "Collection":
        return "Collection"

    return "No Action Detected"


def get_page_content(full_url):
    try:
        req = urllib.request.Request(url=full_url, headers=HEADERS)
        page = urllib.request.urlopen(req).read()

        soup = BeautifulSoup(page, features="html.parser")

        try:
            content_button = soup.find_all("div", class_=BUTTON_TYPE_CLASS)[0].contents[0].contents[0]
        except Exception as e:
            app.logger.error("Except in content_button" + str(e))
            return None, None

        try:
            content_price = soup.find_all("div", class_=PRICE_CLASS)[0]
        except IndexError:  # if no price found
            return None, detect_action(content_button)
        except Exception as e:
            app.logger.error("Except in content_button" + str(e))
            return None

        return content_price, detect_action(content_button)

    # retry on 429
    except HTTPError as e:
        if e.code == 429:
            app.logger.error("retry on 429 " + str(e))
            return e.code, e.code

    except Exception as e:  # general exception
        app.logger.error("Except in get_page_content " + str(e))
        return None, None


def push_to_queue(asset):
    rabbitmq_url = str(os.environ.get('STACKHERO_RABBITMQ_AMQP_URL_TLS'))

    params = pika.URLParameters(rabbitmq_url)
    try:
        connection = pika.BlockingConnection(params)
    except Exception as e:
        app.logger.info("Error in BlockingConnection: " + str(e))
        return

    channel = connection.channel()  # start a channel
    channel.queue_declare(queue='scrape_results')  # Declare a queue
    channel.basic_publish(exchange='',
                          routing_key='scrape_results',
                          body=asset.to_json())

    app.logger.info("Sent: " + asset.to_json())
    connection.close()


port = os.environ.get("PORT", 5000)
app.run(debug=True, host="0.0.0.0", port=port)
