from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

from src.schedule_ai import AssistantConfig, OpenAIConfig, ScheduleAssistant


def _interactive_pick(actions):
    print("\n解析結果：")
    for index, action in enumerate(actions, start=1):
        print(f"{index}. {json.dumps(asdict(action), ensure_ascii=False)}")

    raw = input("輸入要保留的編號，逗號分隔；直接 Enter 代表全部保留：").strip()
    if not raw:
        return set(range(len(actions)))
    selected = {int(part.strip()) - 1 for part in raw.split(",") if part.strip().isdigit()}
    return {index for index in selected if 0 <= index < len(actions)}


def run_colab_demo():
    try:
        from google.colab import files
    except ImportError as exc:
        raise RuntimeError("這個入口是給 Colab 用的") from exc

    print("請上傳 .docx 行程表")
    uploaded = files.upload()
    if not uploaded:
        raise ValueError("尚未上傳任何檔案")

    input_path = next(iter(uploaded.keys()))
    print(f"目前使用檔案：{input_path}")

    print("\n請貼上老闆訊息，多行可直接貼，輸入 END 結束：")
    messages: list[str] = []
    while True:
        line = input().strip()
        if line.upper() == "END":
            break
        if line:
            messages.append(line)

    config = AssistantConfig(
        openai=OpenAIConfig(model="gpt-4.1-mini", enabled=True),
        today=date.today(),
        manual_parse_review=True,
        manual_apply_review=True,
        prune_past_days=True,
    )

    assistant = ScheduleAssistant(config)
    output_path, actions, result = assistant.run(
        input_docx_path=input_path,
        messages=messages,
        parse_review_callback=_interactive_pick,
        apply_review_callback=_interactive_pick,
    )

    print("\n最終操作：")
    for message in result.messages:
        print("-", message)

    print(f"\n已輸出：{output_path}")
    files.download(output_path)


def run_month_completion_demo():
    try:
        from google.colab import files
    except ImportError as exc:
        raise RuntimeError("這個入口是給 Colab 用的") from exc

    print("請上傳要補齊月份表格的 .docx 行程表")
    uploaded = files.upload()
    if not uploaded:
        raise ValueError("尚未上傳任何檔案")

    input_path = next(iter(uploaded.keys()))
    print(f"目前使用檔案：{input_path}")

    month = int(input("請輸入要補齊的月份，例如 4：").strip())
    year_raw = input("若要指定年份請輸入，例如 2026；直接 Enter 代表自動偵測：").strip()
    year = int(year_raw) if year_raw else None

    config = AssistantConfig(
        openai=OpenAIConfig(enabled=False),
        today=date.today(),
        manual_parse_review=False,
        manual_apply_review=False,
        prune_past_days=False,
    )

    assistant = ScheduleAssistant(config)
    output_path, inserted = assistant.complete_month(
        input_docx_path=input_path,
        month=month,
        year=year,
    )

    if inserted:
        print("\n本次補齊的日期：")
        for item in inserted:
            print("-", item)
    else:
        print("\n這個月份沒有缺少日期，不需要補齊。")

    print(f"\n已輸出：{output_path}")
    files.download(output_path)


if __name__ == "__main__":
    run_colab_demo()
