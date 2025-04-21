# webhook_server.py

import logging
import json
import os
from flask import Flask, request, abort, Response
from yookassa import Configuration
from yookassa.domain.notification import WebhookNotification


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


try:
    from db import User, activate_subscription, get_db, SessionLocal
    from config import YOOKASSA_SECRET_KEY
    from sqlalchemy.exc import SQLAlchemyError
except ImportError as e:
    logger.critical(f"Failed to import necessary modules from db/config: {e}")

    raise


app = Flask(__name__)


try:

    if YOOKASSA_SECRET_KEY:
        Configuration.configure(None, YOOKASSA_SECRET_KEY)
        logger.info("Yookassa SDK configured for webhook server.")
    else:
        logger.warning("YOOKASSA_SECRET_KEY not found for webhook server.")
except Exception as e:
    logger.error(f"Failed to configure Yookassa SDK for webhook server: {e}")


@app.route('/yookassa/webhook', methods=['POST'])
def yookassa_webhook():

    yookassa_ips = {
        '185.71.76.0/27',
        '185.71.77.0/27',
        '77.75.153.0/25',
        '77.75.156.11',
        '77.75.156.35',
        '2a02:5180:0:1509::/64',
        '2a02:5180:0:2655::/64',
        '2a02:5180:0:1533::/64',

    }




    try:

        request_body = request.get_data(as_text=True)
        logger.info(f"Received Yookassa webhook: {request_body[:500]}...")


        notification = WebhookNotification(json.loads(request_body))
        payment = notification.object

        logger.info(f"Webhook event: {notification.event}, Payment ID: {payment.id}, Status: {payment.status}")


        if notification.event == 'payment.succeeded' and payment.status == 'succeeded':
            logger.info(f"Processing successful payment: {payment.id}")


            metadata = payment.metadata
            if not metadata or 'telegram_user_id' not in metadata:
                logger.error(f"Webhook error: 'telegram_user_id' not found in metadata for payment {payment.id}")

                return Response(status=200)

            telegram_user_id = metadata['telegram_user_id']
            logger.info(f"Attempting to activate subscription for Telegram User ID: {telegram_user_id}")


            db_session = None
            try:
                db_session = SessionLocal()

                user = db_session.query(User).filter(User.telegram_id == telegram_user_id).first()

                if user:

                    if activate_subscription(db_session, user.id):
                        logger.info(f"Subscription successfully activated for user {telegram_user_id} (DB ID: {user.id}) via webhook for payment {payment.id}.")


                    else:
                        logger.error(f"Failed to activate subscription in DB for user {telegram_user_id} (DB ID: {user.id}) payment {payment.id}.")
                else:
                    logger.error(f"User with Telegram ID {telegram_user_id} not found in DB for payment {payment.id}.")

            except SQLAlchemyError as db_e:
                logger.error(f"Database error during webhook processing for user {telegram_user_id} payment {payment.id}: {db_e}", exc_info=True)
                if db_session:
                    db_session.rollback()
            except Exception as e:
                logger.error(f"Unexpected error during database operation in webhook for user {telegram_user_id} payment {payment.id}: {e}", exc_info=True)
                if db_session and db_session.is_active:
                    db_session.rollback()
            finally:
                if db_session:
                    db_session.close()


        return Response(status=200)

    except json.JSONDecodeError:
        logger.error("Webhook error: Invalid JSON received.")
        abort(400)
    except Exception as e:
        logger.error(f"Unexpected error in webhook handler: {e}", exc_info=True)
        abort(500)


if __name__ == '__main__':


    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
