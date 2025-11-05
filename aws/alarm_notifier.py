import json
import logging
import os
import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Handle SNS events from CloudWatch Alarms and send email notifications via SES.
    """
    try:
        # Get environment variables
        ses_region = os.environ.get('SES_REGION', 'sa-east-1')
        from_email = os.environ.get('FROM_EMAIL')
        alert_emails = os.environ.get('ALERT_EMAILS')
        subject_prefix = os.environ.get('SUBJECT_PREFIX', '[ALERTA agente]')
        
        # Validate required config
        if not from_email or not alert_emails:
            logger.error(json.dumps({
                'status': 'missing-config',
                'from_email': bool(from_email),
                'alert_emails': bool(alert_emails)
            }))
            return {'statusCode': 500, 'body': 'Missing configuration'}
        
        # Parse email list
        email_list = [email.strip() for email in alert_emails.split(',') if email.strip()]
        if not email_list:
            logger.error(json.dumps({'status': 'missing-config', 'reason': 'empty-email-list'}))
            return {'statusCode': 500, 'body': 'No valid email addresses'}
        
        # Configure SES client with short timeouts
        config = Config(
            region_name=ses_region,
            connect_timeout=2,
            read_timeout=10,
            retries={'max_attempts': 2, 'mode': 'standard'}
        )
        ses_client = boto3.client('ses', config=config)
        
        # Process SNS records
        for record in event.get('Records', []):
            sns_data = record.get('Sns', {})
            message_text = sns_data.get('Message', '')
            
            if not message_text:
                continue
                
            try:
                # Parse CloudWatch Alarm message
                alarm_data = json.loads(message_text)
                
                # Extract alarm details
                alarm_name = alarm_data.get('AlarmName', 'Unknown')
                new_state = alarm_data.get('NewStateValue', 'Unknown')
                new_reason = alarm_data.get('NewStateReason', 'No reason provided')
                region = alarm_data.get('Region', 'Unknown')
                
                trigger = alarm_data.get('Trigger', {})
                namespace = trigger.get('Namespace', 'Unknown')
                metric_name = trigger.get('MetricName', 'Unknown')
                dimensions = trigger.get('Dimensions', [])
                
                # Build email content
                subject = f"{subject_prefix} {alarm_name} - {new_state}"
                
                body = f"""Alerta CloudWatch

Alarma: {alarm_name}
Estado: {new_state}
Motivo: {new_reason}
Región: {region}

Métrica:
- Namespace: {namespace}
- Métrica: {metric_name}

Dimensiones:
"""
                
                for dim in dimensions:
                    name = dim.get('name', 'Unknown')
                    value = dim.get('value', 'Unknown')
                    body += f"- {name}: {value}\n"
                
                # Add CloudWatch console link
                console_url = f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#alarmsV2:alarm/{alarm_name}"
                body += f"\nVer en CloudWatch:\n{console_url}"
                
                # Send email
                response = ses_client.send_email(
                    Source=from_email,
                    Destination={'ToAddresses': email_list},
                    Message={
                        'Subject': {'Data': subject, 'Charset': 'UTF-8'},
                        'Body': {'Text': {'Data': body, 'Charset': 'UTF-8'}}
                    }
                )
                
                # Log success
                logger.info(json.dumps({
                    'status': 'email-sent',
                    'alarm': alarm_name,
                    'state': new_state,
                    'recipients': len(email_list),
                    'message_id': response.get('MessageId')
                }))
                
            except json.JSONDecodeError as e:
                logger.error(json.dumps({
                    'status': 'parse-error',
                    'error': str(e),
                    'message_preview': message_text[:100]
                }))
                continue
            except Exception as e:
                logger.error(json.dumps({
                    'status': 'send-error',
                    'alarm': alarm_name if 'alarm_name' in locals() else 'Unknown',
                    'error': str(e)
                }))
                continue
        
        return {'statusCode': 200, 'body': 'Notifications processed'}
        
    except Exception as e:
        logger.error(json.dumps({
            'status': 'handler-error',
            'error': str(e)
        }))
        return {'statusCode': 500, 'body': 'Internal error'}