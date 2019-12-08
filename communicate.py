import datetime
import os
import pickle
import warnings


MIN_PORT_NUMBER = 1024
MAX_PORT_NUMBER = 65535
DEFAULT_PORT_NUMBER = 50007

MAX_MSG_SIZE = 2 ** 20
BUFFER_SIZE = 1024
NUM_BYTES_FOR_MSG_LENGTH = 4
MSG_BYTEORDER = 'big'

LOGDIR = 'logs'
CORRUPTED_MESSAGES_DIR = os.path.join(LOGDIR, 'corrupted_messages')
CORRUPTED_MSG_FILE_TMPL = "{ip}:{port}_{dt}.bin"
# Максимальное разрешенное число дампов поврежденных сообщений, принятых от
# одного игрока в одно время. Подобные ситуации при правильной работе
# программы не должны возникать.
MAX_NUM_CORRUPTED_MSG_FILES = 5
OUT_OF_BOUNDS_CORRUPTED_MSG_TMPL = (
    "Сообщение, начинающееся с байта с индексом {start}, не соответствует " 
    "протоколу. Длина сообщения, начинающегося с {start}-го байта " 
    "выходит за границу массива принятых данных. Это может быть связано с "
    "обрывом связи при передаче сообщения, распределенного по 2м или более "
    "буферам. Процесс приема сообщений останавлен.\n"
    "len(data) = {data_length}\n"
    "отправитель: {addr}\n"
    "Длина закодированного сообщения: {length}\n"
    "Принятые данные сохранены в файл {dump_fn}'"
)
UNPICKLING_CORRUPTED_MSG_TMPL = (
    "Сообщение, начинающееся с байта с индексом {start}, не соответствует "
    "протоколу. Невозможно выполнить unpickling данных со {start_pickled}-го "
    "по {end_pickled}-й байты не включительно. Процесс приема данных "
    "останавлен.\n"
    "отправитель: {addr}\n"
    "Длина закодированного сообщения: {length}\n"
    "Принятые данные сохранены в файл {dump_fn}'"
)

CONNECTION_ABORTED_ERROR_WARNING = "Возможная причина ошибки " \
    "`ConnectionAbortedError` -- вмешательство антивируса. Однако чаще " \
    "всего она возникает при обрыве соединения клиентом."


class CorruptedMessageError(Exception):
    def __init__(self, msg, data, idx, length):
        self.message = msg
        self.data = data
        self.idx = idx
        self.length = length


def get_dump_fn_for_corrupted_data(addr):
    fn = CORRUPTED_MSG_FILE_TMPL.format(
        ip=addr[0], port=addr[1], dt=datetime.datetime.now())
    path = os.path.join(CORRUPTED_MESSAGES_DIR, fn)
    if os.path.exists(path):
        i = 1
        base, ext = os.path.splitext(path)
        tmpl = base + "#{}" + ext
        path = tmpl.format(i)
        while os.path.exists(path) and i < MAX_NUM_CORRUPTED_MSG_FILES:
            i += 1
            tmpl.format(i)
        if i >= MAX_NUM_CORRUPTED_MSG_FILES:
            raise ValueError(
                "Превышение лимита сохраняемых поврежденных сообщений. "
                "См. файл {}.".format(path))
    return path


def dump_corrupted_data(data, dump_fn):
    dir_ = os.path.split(dump_fn)[0]
    if dir:
        os.makedirs(dir_, exist_ok=True)
    with open(dump_fn, 'wb') as f:
        f.write(data)


def send_data(conn, data):
    data = pickle.dumps(data)
    length = len(data)
    if length > MAX_MSG_SIZE:
        raise ValueError(
            "Размер закодированного объекта для отправки равен {} байт, в то "
            "время как максимально допустимый размер составляет {}".format(
                length, MAX_MSG_SIZE))
    msg = len(data).to_bytes(NUM_BYTES_FOR_MSG_LENGTH, MSG_BYTEORDER)
    conn.sendall(msg + data)


def parse_received(conn, data, addr):
    msgs = []
    i = 0
    error_msg = None
    while i < len(data):
        length_encoded = data[i: i + NUM_BYTES_FOR_MSG_LENGTH]
        length = int.from_bytes(length_encoded, MSG_BYTEORDER)
        if i + length + NUM_BYTES_FOR_MSG_LENGTH > len(data):
            dump_fn = get_dump_fn_for_corrupted_data(addr)
            dump_corrupted_data(data, dump_fn)
            error_msg = OUT_OF_BOUNDS_CORRUPTED_MSG_TMPL.format(
                start=i,
                length_end=i+NUM_BYTES_FOR_MSG_LENGTH,
                data_length=len(data),
                addr=addr,
                length=length,
                dump_fn=dump_fn,
            )
            break
        i += NUM_BYTES_FOR_MSG_LENGTH
        try:
            msg = pickle.loads(data[i: i+length])
        except pickle.UnpicklingError:
            dump_fn = get_dump_fn_for_corrupted_data(addr)
            dump_corrupted_data(data, dump_fn)
            error_msg = UNPICKLING_CORRUPTED_MSG_TMPL.format(
                    start=i-NUM_BYTES_FOR_MSG_LENGTH,
                    start_pickled=i,
                    end_pickled=i+length,
                    addr=addr,
                    length=length,
                    dump_fn=dump_fn
                )
            break
        i += length
        msgs.append(msg)
    if error_msg is not None:
        send_data(
            conn,
            {
                'type': 'error_msg',
                'error_class': 'CorruptedMessageError',
                'msg': error_msg,
                'data': data,
                'i': i,
                'length': length
            }
        )
        raise CorruptedMessageError(msg, data, i, length)
    return msgs


def recv_data(conn, addr):
    data = b''
    error_instance = None
    buffer = b''
    while True:
        try:
            buffer = conn.recv(BUFFER_SIZE)
        except Exception as e:
            error_instance = e
        if buffer is None or not buffer:
            break
        data += buffer
        buffer = b''
        if error_instance is not None:
            break
    return parse_received(conn, data, addr), error_instance


def send_data_quite(conn, addr, data):
    try:
        send_data(conn, data)
    except BrokenPipeError as e:
        warnings.warn(e)
        warn_no_msg_was_sent(data, addr)
    except ConnectionAbortedError as e:
        warnings.warn(e)
        warnings.warn(CONNECTION_ABORTED_ERROR_WARNING)
        warn_no_msg_was_sent(data, addr)
    except Exception as e:
        warnings.warn(e)
        warnings.warn(
            "Для исключения типа {} не был написан обработчик. Возможно, "
            "стоит это сделать.".format(type(e))
        )
        warn_no_msg_was_sent(data, addr)


def warn_no_msg_was_sent(msg, addr):
    warnings.warn(
        "Сообщение адресату {} не было отправлено.\n"
        "Сообщение:\n{}".format(addr, msg)
    )
