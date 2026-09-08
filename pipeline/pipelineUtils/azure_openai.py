from openai import AzureOpenAI
import logging
from azure.identity import get_bearer_token_provider
from pipelineUtils.db import save_chat_message
from configuration import Configuration

config = Configuration()

OPENAI_API_BASE = config.get_value("OPENAI_API_BASE")
OPENAI_MODEL = config.get_value("OPENAI_MODEL")
OPENAI_API_VERSION = config.get_value("OPENAI_API_VERSION")


class RequestTooLargeError(Exception):
    """Raised when an Azure OpenAI request exceeds the request-size limit."""


def run_prompt(
    pipeline_id,
    system_prompt,
    user_prompt,
    base64_images=None,
    log_interaction=True,
):
    token_provider = get_bearer_token_provider(  
        config.credential,  
        "https://cognitiveservices.azure.com/.default"  
    )  

    token = config.credential.get_token("https://cognitiveservices.azure.com/.default").token
    
    openai_client = AzureOpenAI(
        azure_ad_token=token,
        api_version = OPENAI_API_VERSION,
        azure_endpoint =OPENAI_API_BASE
    )

    if log_interaction:
        logging.info(f"User Prompt: {user_prompt}")
        logging.info(f"System Prompt: {system_prompt}")

        save_chat_message(pipeline_id, "system", system_prompt)
        save_chat_message(pipeline_id, "user", user_prompt)

    try:
        if base64_images:
            user_content = [{"type": "text", "text": user_prompt}]
            user_content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image}"}
                }
                for image in base64_images
            )
            user_message = {"role": "user", "content": user_content}
        else:
            user_message = {"role": "user", "content": user_prompt}

        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{ "role": "system", "content": system_prompt},
                user_message])
        assistant_msg = response.choices[0].message.content
        usage = {
            "prompt_tokens":   response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
            "total_tokens":    response.usage.total_tokens,
            "model":           response.model
        }

        if log_interaction:
            save_chat_message(pipeline_id, "assistant", assistant_msg, usage)
        return assistant_msg
    
    except Exception as e:
        if getattr(e, "status_code", None) == 413:
            raise RequestTooLargeError(
                "Azure OpenAI request exceeded the allowed size"
            ) from e
        logging.error(f"Error calling OpenAI API: {e}")
        raise  # Re-raise to allow Durable Functions to retry


