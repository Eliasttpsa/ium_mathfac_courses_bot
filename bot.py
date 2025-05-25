import requests, re, time, logging, hashlib
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from urllib.parse import urljoin
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)


# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


TOKEN = ""  # <- сюда вставлять токен своего бота
BASE_URL = "https://mccme.ru"

# Сюда можно засунуть url семестров чтобы оно их обработало 
# (работает пока только с последним весенним, так как нму поменяли недавно структуру сайта)
SEMESTERS = [
    {"title": "Весна 2024-2025", "url": f"{BASE_URL}/ru/nmu/courses-of-nmu/vesna-20242025/"},
]

# Pretty self-explanatory
class CourseCache:
    def __init__(self):
        self.courses = {}
        self.cache_timeout = 3600  # 1 час
    
    def get_cached_courses(self, semester_url):
        if semester_url in self.courses:
            cache_data = self.courses[semester_url]
            if time.time() - cache_data['timestamp'] < self.cache_timeout:
                return cache_data['data']
        return None
    
    def cache_courses(self, semester_url, courses):
        self.courses[semester_url] = {
            'data': courses,
            'timestamp': time.time()
        }

course_cache = CourseCache()

def generate_short_id(url):
    """Генерирует короткий ID для URL"""
    return hashlib.md5(url.encode()).hexdigest()[:8]

async def fetch_courses(semester_url):
    try:
        # Проверяем кэш
        cached_courses = course_cache.get_cached_courses(semester_url)
        if cached_courses:
            logger.info(f"Используем кэшированные курсы для {semester_url}")
            return cached_courses
        
        logger.info(f"Загружаем курсы для семестра: {semester_url}")
        response = requests.get(semester_url, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        courses = []
        
        # Ищем все ссылки в основном контенте
        main_content = soup.select_one('.page-content, .main-section')
        if not main_content:
            main_content = soup
            
        for link in main_content.select('a[href]'):
            href = link['href']
            text = link.get_text(strip=True)
            
            # Фильтруем только ссылки на курсы
            if (href.startswith('/ru/nmu/') or ('course' in href)) and len(text) > 5:
                full_url = urljoin(BASE_URL, href)
                
                # Дополнительная фильтрация
                if any(x in text.lower() for x in ['архив', 'разные годы', 'другие']):
                    continue
                    
                courses.append({
                    'title': text,
                    'url': full_url,
                    'id': generate_short_id(full_url)
                })
        
        # Удаляем дубликаты
        unique_courses = []
        seen_ids = set()
        for course in courses:
            if course['id'] not in seen_ids:
                seen_ids.add(course['id'])
                unique_courses.append(course)
        
        # Сортируем курсы по алфавиту
        unique_courses.sort(key=lambda x: x['title'].lower())
        
        if not unique_courses:
            raise ValueError(f"Не найдено курсов для семестра")
        
        logger.info(f"Найдено {len(unique_courses)} курсов")
        course_cache.cache_courses(semester_url, unique_courses)
        return unique_courses
    
    except Exception as e:
        logger.error(f"Ошибка при получении курсов: {e}")
        cached_courses = course_cache.get_cached_courses(semester_url)
        if cached_courses:
            return cached_courses
        raise


async def fetch_course_details(course_id, courses_list):
    """Функция парсит инфу о курсе"""
    course = next((c for c in courses_list if c['id'] == course_id), None)
    if not course:
        raise ValueError("Курс не найден")

    try:
        logger.info(f"Загружаем детали курса: {course['url']}")
        response = requests.get(course['url'], timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. Основная информация
        course_title = soup.select_one('.course-discipline p').get_text(strip=True) if soup.select_one('.course-discipline p') else course['title']
        teacher = soup.select_one('.course-teacher p').get_text(strip=True) if soup.select_one('.course-teacher p') else ""

        # 2. Расписание
        schedule = []
        time_block = soup.select_one('.course-time')
        if time_block:
            schedule_text = ' '.join(time_block.get_text().split())
            schedule_text = schedule_text.split(".")[0]
            schedule.append(schedule_text)
        
        # 3. Программа курса 
        program = []
        
        ol_items = soup.select('.course-program ol li, .program-content ol li, .wrapper ol li')
        if ol_items:
            program = [f"{i+1}. {item.get_text(strip=True)}" for i, item in enumerate(ol_items)]
        else:
            numbered_items = soup.find_all(string=re.compile(r'^\d+[\.\)]'))
            if numbered_items:
                program = [item.strip() for item in numbered_items]
            else:
                program_block = soup.select_one('.course-program, .program-content, .syllabus')
                if program_block:
                    text = program_block.get_text()
                    program = [f"{i+1}. {item.strip()}" for i, item in enumerate(
                        filter(None, re.split(r'\n\s*-|\n\s*\d+[\.\)]', text)))
                        if item.strip()][:20]  # Ограничение 20 пунктов
        
        # 4. Плейлисты
        playlists = {
            'youtube': next((a['href'] for a in soup.select('a[href*="youtube.com/playlist"]')), None),
            'rutube': next((a['href'] for a in soup.select('a[href*="rutube.ru/plst"]')), None)
        }

        # 5. Материалы
        materials = []
        for a in soup.select('a[href$=".pdf"], a[href$=".pptx"], a[href$=".docx"]'):
            materials.append({
                'title': a.get_text(strip=True) or a['href'].split('/')[-1],
                'url': urljoin(course['url'], a['href'])
            })

        # Формирование сообщения
        message_parts = [
            f"<b>📚 {course_title}</b>",
            f"\n<b>Преподаватель:</b> {teacher}" if teacher else "",
            "\n<b>Расписание:</b>\n" + "\n".join(f"- {item}" for item in schedule) if schedule else "",
            "\n<b>Программа курса:</b>\n" + "\n".join(program[:15]) if program else "",
        ]
        
        if playlists['youtube'] or playlists['rutube']:
            message_parts.append("\n<b>Видеолекции:</b>")
            if playlists['youtube']:
                message_parts.append(f"\n🎬 <a href='{playlists['youtube']}'>YouTube</a>")
            if playlists['rutube']:
                message_parts.append(f"\n🎥 <a href='{playlists['rutube']}'>Rutube</a>")

        return "".join(message_parts)[:4000], materials

    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        return f"<b>📚 {course['title']}</b>\n\nНе удалось загрузить информацию", []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Начало работы бота
    try:
        keyboard = [
            [InlineKeyboardButton(semester['title'], 
                                  callback_data=f"sem_{generate_short_id(semester['url'])}")]
            for semester in SEMESTERS
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "📅 Выберите учебный семестр:",
            reply_markup=reply_markup
        )
    except Exception as e:
        await update.message.reply_text(
            "⚠ Ошибка при загрузке меню. Попробуйте позже.")

async def show_semester_courses(update: Update, 
                                context: ContextTypes.DEFAULT_TYPE):
    """Функция отобрадает все курсы данного семестра"""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("sem_"):
        semester_id = query.data[4:]
        semester = next((s for s in SEMESTERS 
                         if generate_short_id(s['url']) == semester_id), None)
        
        if not semester:
            await query.edit_message_text("⚠ Семестр не найден")
            return
        
        try:
            courses = await fetch_courses(semester['url'])
            
            
            keyboard = [
                [InlineKeyboardButton(course['title'], callback_data=f"crs_{course['id']}")]
                for course in courses
            ]
            keyboard.append([InlineKeyboardButton("🔙 Назад к семестрам", 
                                                  callback_data="back_sem")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"📚 Курсы семестра {semester['title']} (по алфавиту):",
                reply_markup=reply_markup
            )
            
            # Сохраняем список курсов в контексте
            context.user_data['current_courses'] = courses
            
        except Exception as e:
            logger.error(f"Ошибка при загрузке курсов: {e}")
            await query.edit_message_text(
                "⚠ Не удалось загрузить курсы. Попробуйте позже.")

async def handle_course_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Функция выводит информацию о курсе, или возвращает к выбору семестра"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "back_sem":
        await start(update, context)
        return
    
    if query.data.startswith("crs_"):
        course_id = query.data[4:]
        courses = context.user_data.get('current_courses', [])
        
        try:
            await query.edit_message_text("⏳ Загружаю информацию о курсе...")
            description, materials = await fetch_course_details(course_id, courses)
            
            message_text = f"📚 <b>Информация о курсе</b>\n\n{description}"
            await query.edit_message_text(
                text=message_text,
                parse_mode='HTML'
            )
            
            if materials:
                materials_text = "\n".join([f"📄 <a href='{m['url']}'>{m['title']}</a>" 
                                            for m in materials])
                await query.message.reply_text(
                    f"🔗 <b>Материалы курса:</b>\n\n{materials_text}",
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            else:
                await query.message.reply_text("ℹ️ Материалы к курсу не найдены")
        
        except Exception as e:
            logger.error(f"Ошибка обработки курса: {e}")
            await query.edit_message_text(
                "⚠ Не удалось загрузить информацию о курсе. Попробуйте позже.")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    logger.error(f"Ошибка в обработчике: {context.error}", exc_info=True)

    if update.callback_query:
        await update.callback_query.message.reply_text(
            "⚠ Произошла ошибка. Попробуйте позже.")
    elif update.message:
        await update.message.reply_text(
            "⚠ Произошла ошибка. Попробуйте позже.")

def main():
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(show_semester_courses, 
                                         pattern="^sem_"))
    
    app.add_handler(CallbackQueryHandler(handle_course_selection, 
                                         pattern="^crs_|^back_sem$"))
    
    app.add_error_handler(error_handler)
    
    logger.info("Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()