import os
import requests
import schedule
import time
import logging
import re
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pytz
from telegram import Bot
from telegram.error import TelegramError

# 환경변수 로드
load_dotenv()

class CloudDartTelegramNotifier:
    def __init__(self):
        # 환경변수에서 API 키 가져오기
        self.dart_api_key = os.getenv('DART_API_KEY')
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')

        if not all([self.dart_api_key, self.telegram_token, self.chat_id]):
            raise ValueError("필수 환경변수가 설정되지 않았습니다: DART_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID")

        self.bot = Bot(token=self.telegram_token)
        self.base_url = "https://opendart.fss.or.kr/api/list.json"

        # 로깅 설정
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

        # 한국 시간대 설정
        self.kst = pytz.timezone('Asia/Seoul')

        # 지분공시 관련 키워드
        self.equity_keywords = [
            '지분공시', '주식등의대량보유상황보고서', '임원ㆍ주요주주특정증권등소유상황보고서',
            '주요주주변동신고서', '5%룰', '대량보유', '특정증권등소유상황보고서'
        ]

        # 매수 관련 키워드
        self.purchase_keywords = ['매수', '취득', '증가', '신규취득', '추가취득']
        self.sale_keywords = ['매도', '처분', '감소', '양도']

    def get_today_disclosures(self):
        """오늘 공시 데이터 수집"""
        try:
            today = datetime.now(self.kst).strftime('%Y%m%d')

            params = {
                'crtfc_key': self.dart_api_key,
                'bgn_de': today,
                'end_de': today,
                'page_no': 1,
                'page_count': 100
            }

            response = requests.get(self.base_url, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()

            if data.get('status') == '000':
                return data.get('list', [])
            else:
                self.logger.warning(f"DART API 응답 오류: {data.get('message', 'Unknown error')}")
                return []

        except Exception as e:
            self.logger.error(f"공시 데이터 수집 중 오류: {str(e)}")
            return []

    def filter_equity_disclosures(self, disclosures):
        """지분공시 관련 보고서 필터링"""
        equity_disclosures = []

        for disclosure in disclosures:
            report_nm = disclosure.get('report_nm', '')
            rm = disclosure.get('rm', '')

            # 지분공시 관련 키워드 확인
            is_equity = any(keyword in report_nm for keyword in self.equity_keywords)

            if is_equity:
                equity_disclosures.append(disclosure)

        return equity_disclosures

    def analyze_purchase_activity(self, disclosures):
        """매수/매도 활동 분석"""
        purchase_activities = []

        for disclosure in disclosures:
            report_nm = disclosure.get('report_nm', '')
            rm = disclosure.get('rm', '')
            flr_nm = disclosure.get('flr_nm', '')

            # 매수/매도 키워드 점수 계산
            purchase_score = sum(1 for keyword in self.purchase_keywords if keyword in rm or keyword in report_nm)
            sale_score = sum(1 for keyword in self.sale_keywords if keyword in rm or keyword in report_nm)

            # 매수 활동으로 판단되는 경우
            if purchase_score > sale_score and purchase_score > 0:
                # 금융 정보 추출
                financial_info = self.extract_financial_info(rm)

                activity = {
                    'corp_name': disclosure.get('corp_name', ''),
                    'stock_code': disclosure.get('stock_code', ''),
                    'flr_nm': flr_nm,
                    'report_nm': report_nm,
                    'rcept_dt': disclosure.get('rcept_dt', ''),
                    'rm': rm,
                    'purchase_score': purchase_score,
                    'financial_info': financial_info
                }
                purchase_activities.append(activity)

        return purchase_activities

    def extract_financial_info(self, text):
        """비고란에서 수량, 금액, 지분율 정보 추출"""
        info = {}

        # 수량 추출 (주, 좌 단위)
        quantity_patterns = [
            r'(\d{1,3}(?:,\d{3})*)\s*주',
            r'(\d{1,3}(?:,\d{3})*)\s*좌',
            r'수량[:\s]*(\d{1,3}(?:,\d{3})*)'
        ]

        for pattern in quantity_patterns:
            match = re.search(pattern, text)
            if match:
                info['quantity'] = match.group(1)
                break

        # 금액 추출 (원, 억원 단위)
        amount_patterns = [
            r'(\d{1,3}(?:,\d{3})*)\s*억\s*원',
            r'(\d{1,3}(?:,\d{3})*)\s*원',
            r'금액[:\s]*(\d{1,3}(?:,\d{3})*)'
        ]

        for pattern in amount_patterns:
            match = re.search(pattern, text)
            if match:
                info['amount'] = match.group(1)
                if '억' in pattern:
                    info['amount_unit'] = '억원'
                else:
                    info['amount_unit'] = '원'
                break

        # 지분율 추출
        ratio_patterns = [
            r'(\d+\.?\d*)\s*%',
            r'지분율[:\s]*(\d+\.?\d*)'
        ]

        for pattern in ratio_patterns:
            match = re.search(pattern, text)
            if match:
                info['ratio'] = match.group(1) + '%'
                break

        return info

    def create_telegram_message(self, purchase_activities, total_disclosures, equity_count):
        """텔레그램 메시지 생성"""
        today_str = datetime.now(self.kst).strftime('%Y년 %m월 %d일')

        message = f"📊 **DART 지분공시 분석 리포트**\n"
        message += f"📅 **분석일자**: {today_str}\n"
        message += f"📈 **총 공시**: {total_disclosures}건 | **지분공시**: {equity_count}건\n\n"

        if purchase_activities:
            message += f"💰 **주요 매수 현황** ({len(purchase_activities)}건)\n"
            message += "=" * 40 + "\n"

            for i, activity in enumerate(purchase_activities[:10], 1):  # 최대 10건만 표시
                message += f"**{i}. {activity['corp_name']}**\n"
                message += f"   👤 매수자: {activity['flr_nm']}\n"

                financial_info = activity['financial_info']
                if financial_info.get('quantity'):
                    message += f"   📊 수량: {financial_info['quantity']}주\n"
                if financial_info.get('amount'):
                    unit = financial_info.get('amount_unit', '원')
                    message += f"   💵 금액: {financial_info['amount']}{unit}\n"
                if financial_info.get('ratio'):
                    message += f"   📈 지분율: {financial_info['ratio']}\n"

                message += f"   📋 보고서: {activity['report_nm']}\n"
                message += "\n"

            if len(purchase_activities) > 10:
                message += f"... 외 {len(purchase_activities) - 10}건 더\n\n"
        else:
            message += "💡 **오늘은 주목할 만한 매수 활동이 없습니다.**\n\n"

        # 특이사항 분석
        if purchase_activities:
            # 가장 활발한 매수자
            flr_counts = {}
            for activity in purchase_activities:
                flr = activity['flr_nm']
                flr_counts[flr] = flr_counts.get(flr, 0) + 1

            if flr_counts:
                most_active = max(flr_counts.items(), key=lambda x: x[1])
                if most_active[1] > 1:
                    message += f"🔥 **가장 활발한 매수자**: {most_active[0]} ({most_active[1]}건)\n"

        message += "\n📱 *DART 자동 분석 시스템*"

        return message

    async def send_telegram_message(self, message):
        """텔레그램 메시지 전송"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode='Markdown'
            )
            self.logger.info("텔레그램 메시지 전송 완료")
            return True
        except TelegramError as e:
            self.logger.error(f"텔레그램 메시지 전송 실패: {str(e)}")
            return False

    def run_daily_analysis(self):
        """일일 분석 실행"""
        try:
            self.logger.info("일일 DART 분석 시작")

            # 1. 오늘 공시 데이터 수집
            all_disclosures = self.get_today_disclosures()
            self.logger.info(f"총 {len(all_disclosures)}건의 공시 수집")

            # 2. 지분공시 필터링
            equity_disclosures = self.filter_equity_disclosures(all_disclosures)
            self.logger.info(f"지분공시 {len(equity_disclosures)}건 필터링")

            # 3. 매수 활동 분석
            purchase_activities = self.analyze_purchase_activity(equity_disclosures)
            self.logger.info(f"매수 활동 {len(purchase_activities)}건 분석")

            # 4. 텔레그램 메시지 생성 및 전송
            message = self.create_telegram_message(
                purchase_activities, 
                len(all_disclosures), 
                len(equity_disclosures)
            )

            # 비동기 메시지 전송을 위한 이벤트 루프 처리
            import asyncio
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            success = loop.run_until_complete(self.send_telegram_message(message))

            if success:
                self.logger.info("일일 분석 및 알림 전송 완료")
            else:
                self.logger.error("텔레그램 알림 전송 실패")

        except Exception as e:
            self.logger.error(f"일일 분석 중 오류 발생: {str(e)}")

def main():
    """메인 함수"""
    try:
        notifier = CloudDartTelegramNotifier()

        # 한국 시간 기준으로 매일 15시에 실행
        schedule.every().day.at("15:00").do(notifier.run_daily_analysis)

        notifier.logger.info("DART 텔레그램 알림 시스템 시작")
        notifier.logger.info("매일 15시(KST)에 분석 실행 예정")

        # 시작 시 테스트 실행 (선택사항)
        if os.getenv('RUN_TEST_ON_START', 'false').lower() == 'true':
            notifier.logger.info("시작 시 테스트 실행")
            notifier.run_daily_analysis()

        # 스케줄러 실행
        while True:
            schedule.run_pending()
            time.sleep(60)  # 1분마다 체크

    except KeyboardInterrupt:
        print("\n프로그램이 사용자에 의해 중단되었습니다.")
    except Exception as e:
        print(f"프로그램 실행 중 오류: {str(e)}")

if __name__ == "__main__":
    main()
