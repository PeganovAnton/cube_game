import argparse
import datetime
import os
import pickle
import re
import socket
import tkinter as tk
import warnings
from random import randrange as rnd, choice

import colors


DT = 30
WINDOW_SHAPE = (800, 600)

MIN_PORT_NUMBER = 1024
MAX_PORT_NUMBER = 65535
DEFAULT_PORT_NUMBER = 50007

MAX_NUM_PLAYERS = 10

MAX_NUM_CUBES = 20
DEFAULT_NUM_CUBES = 5

MAX_SEND_DATA_SIZE = 2 ** 20
BUFFER_SIZE = 1024
NUM_BYTES_FOR_MSG_LENGTH = 4

LOGDIR = 'logs'
CORRUPTED_MESSAGES_DIR = os.path.join(LOGDIR, 'corrupted_messages')
CORRUPTED_MSG_FILE_TMPL = "{ip}:{port}_{dt}.bin"
# Максимальное разрешенное число дампов поврежденных сообщений, принятых от
# одного игрока в одно время. Подобные ситуации при правильной работе
# программы не должны возникать.
MAX_NUM_CORRUPTED_MSG_FILES = 5
OUT_OF_BOUNDS_CORRUPTED_MSG_TMPL = (
    "Сообщение, начинающееся с байта с индексом {start}, не соответствует " 
    "протоколу. Длина сообщения, закодированная в байтах с {start}-го по " 
    "{length_end}-й (не включительно) настолько велика, что сообщение не " 
    "может уместиться в принятых данных. Процесс приема сообщений " 
    "останавлен.\n"
    "len(data) = {data_length}\n"
    "отправитель: {addr}\n"
    "Длина закодированного сообщения: {length}\n"
    "Принятые данные сохранены в файл {dump_fn}'"
)
UNPICKLING_CLORRUPTED_MSG_TMPL = (
    "Сообщение, начинающееся с байта с индексом {start}, не соответствует "
    "протоколу. Невозможно выполнить unpickling данных с {start_pickled}-го "
    "по {end_pickled}-й не включительно). Процесс приема данных останавлен.\n"
    "отправитель: {addr}\n"
    "Длина закодированного сообщения: {length}\n"
    "Принятые данные сохранены в файл {dump_fn}'"
)


def get_app_args():
    parser = argparse.ArgumentParser(
        "Это скрипт для запуска игры 'Cube Game'. Игра позволяет схватить "
        "мышкой один из кубиков и двигать его туда сюда. В игре может "
        "участвовать до {} игроков. Чтобы играть, необходимо: (1)запустить "
        "скрипт в режиме 'server', указав при этом число кубиков, "
        "(2)запустить скрипт в режиме 'client' на компьютерах каждого из "
        "игроков и передать при этом ip сервера. ip и порт сервера печатаются "
        "при запуске сервера. Если Вы играете на той же машине, на которой "
        "запущен сервер, то ip при запуске клиента можно не указывать.".format(
            MAX_NUM_PLAYERS)
    )
    parser.add_argument(
        'mode',
        help="Режим в котором запускается скрипт. Возможны варианты 'server', "
             "'client'."
    )
    parser.add_argument(
        '--server_ip',
        '-i',
        help="IPv4 сервера, если скрипт запускается в режиме 'client'. Если "
             "скрипт запущен в режиме 'server', то ip можно не указывать. "
             "Если скрипт запускается в режиме 'client' на той же машине, что "
             "и сервер, то ip можно не указывать.",
        default='localhost'
    )
    parser.add_argument(
        "--server_port",
        "-p",
        help="Порт сервера, через который осуществляется соединение. "
             "Разрешенные значения: {} - {}. Значение по умолчанию "
             "{}.".format(
                 MIN_PORT_NUMBER, MAX_PORT_NUMBER, DEFAULT_PORT_NUMBER),
        type=int,
        default=DEFAULT_PORT_NUMBER
    )
    parser.add_argument(
        "--num_cubes",
        "-n",
        help="Число кубиков в игре. Макчимальное число кубиков: {}. "
             "Значение по умолчанию {}.".format(
                 MAX_NUM_CUBES, DEFAULT_NUM_CUBES),
        type=int,
        default=DEFAULT_NUM_CUBES
    )
    return parser.parse_args()


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
    if length > MAX_SEND_DATA_SIZE:
        raise ValueError(
            "Размер закодированного объекта для отправки равен {} байт, в то "
            "время как максимально допустимый размер составляет {}".format(
                length, MAX_SEND_DATA_SIZE))
    msg = len(data).to_bytes(NUM_BYTES_FOR_MSG_LENGTH, 'big')
    conn.sendall(msg + data)


def parse_received(conn, data, addr):
    msgs = []
    i = 0
    msg = None
    while i < len(data):
        length = data[i: i + NUM_BYTES_FOR_MSG_LENGTH]
        length = int.from_bytes(length, 'big')
        if i + length + NUM_BYTES_FOR_MSG_LENGTH > len(data):
            dump_fn = get_dump_fn_for_corrupted_data(addr)
            dump_corrupted_data(data, dump_fn)
            msg = OUT_OF_BOUNDS_CORRUPTED_MSG_TMPL.format(
                start=i,
                length_end=i+NUM_BYTES_FOR_MSG_LENGTH,
                data_length=len(data),
                addr=addr,
                length=length,
                dump_fn=dump_fn
            )
            break
        i += NUM_BYTES_FOR_MSG_LENGTH
        try:
            msg = pickle.loads(data[i: i+length])
        except pickle.UnpicklingError:
            dump_fn = get_dump_fn_for_corrupted_data(addr)
            dump_corrupted_data(data, dump_fn)
            msg = UNPICKLING_CLORRUPTED_MSG_TMPL.format(
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
    if msg is not None:
        send_data(
            conn,
            {
                'type': 'error_msg',
                'error_class': 'CorruptedMessageError',
                'msg': msg,
                'data': data,
                'i': i,
                'length': length
            }
        )
        raise CorruptedMessageError(msg, data, i, length)
    return msgs


def recv_data(conn, addr):
    data = b''
    while True:
        buffer = conn.recv(BUFFER_SIZE)
        if not buffer:
            break
        data += buffer
    return parse_received(conn, data, addr)


class Cube:
    def __init__(self, cube_canvas, x, y, size, color):
        self.cube_canvas = cube_canvas
        self.x = x
        self.y = y
        self.size = size
        self.color = color

        self.id = self.cube_canvas.create_rectangle(
            self.x,
            self.y,
            self.x + self.size,
            self.y + self.size,
            fill=self.color
        )
        self.cube_canvas.cubes[self.id] = self


class CubeCanvas(tk.Canvas):
    def __init__(self, master, num_cubes):
        super().__init__(master)

        # Кубики располагаются внутри экземпляра `CubeCanvas` случайным
        # образом, но не ближе, чем `self.margin` к границе экземпляра
        # `CubeCanvas`.
        self.margin = 100
        self.cube_init_xrange = [
            self.winfo_rootx() + self.margin,
            self.winfo_rootx() + self.winfo_width() - self.margin
        ]
        self.cube_init_yrange = [
            self.winfo_rooty() + self.margin,
            self.winfo_rooty() + self.winfo_height() - self.margin
        ]
        self.cube_size_range = [15, 75]

        self.num_cubes = num_cubes
        # Ключи в словаре -- id объектов.
        self.cubes = {}
        if self.get_mode() == 'server':
            self.create_cubes()
        else:
            self.waiting_conn_text_id = self.create_text(
                self.winfo_rootx() + self.winfo_width() / 2,
                self.winfo_rooty() + self.winfo_height() / 2,
                text='Ожидание соединения с сервером',
                font='28'
            )

    def get_root(self):
        root = self.master
        while root.master is not None:
            root = root.master
        return root

    def get_mode(self):
        return self.get_root().mode

    def create_cubes(self):
        for _ in range(self.num_cubes):
            x = rnd(*self.cube_init_xrange)
            y = rnd(*self.cube_init_yrange)
            size = rnd(*self.cube_size_range)
            color = choice(colors.INTENSIVE_RAINBOW)
            Cube(self, x, y, size, color)

    def bind_events(self):
        pass  # TODO

    def process_msg(self, login, event_descr):
        pass  # TODO


class MainFrame(tk.Frame):
    def __init__(self, master, num_cubes):
        super().__init__(master)
        self.cube_canvas = CubeCanvas(self, num_cubes)
        self.cube_canvas.pack(fill=tk.BOTH, expand=1)

    def process_msg(self, login, event_descr):
        self.cube_canvas.process_msg(login, event_descr)


class CubeGameApp(tk.Tk):
    def __init__(self, config):
        self.check_config(config)
        super().__init__()

        self.mode = config['mode']
        self.server_ip = config['server_ip']
        self.server_port = config['server_port']

        self.msg_types_common = ['error_msg', 'get_login']
        self.msg_types_from_client = ['event']

        if self.mode == 'server':
            # Спрячем окно, так как серверу оно не нужно.
            self.wm_withdraw()
        else:
            self.geometry('{}x{}'.format(*WINDOW_SHAPE))

        self.main_frame = MainFrame(self, config['num_cubes'])
        self.main_frame.pack(fill=tk.BOTH, expand=1)

        # `self.listener` -- сокет для установления соединения с клиентами.
        # Используется только в режиме 'server'.
        if self.mode == 'server':
            self.listener = socket.socket()
            self.listener.settimeout(0)
            self.listener.bind(('', self.server_port))
            self.listener.listen(MAX_NUM_PLAYERS)
            print(socket.gethostname(), self.server_port)
        else:
            self.listener = None

        # Сокет для обмена данными с сервером.
        # Используется только в режиме 'client'.
        self.conn_to_server = None

        # Словарь сокетов для обмена данными с клиентами.
        # Используется только в режиме 'server'.
        # Ключи в словаре -- логины игроков, значения -- кортежи из 2-х
        # элементов: сокета и адрес. В свою очередь, адрес -- это кортеж из
        # ip и номера порта.
        self.conns_to_clients = {}
        self.connect_to_clients_job = None
        self.receive_from_client_jobs = {}
        self.get_player_logins_jobs = {}

        # ip адреса игроков -- ключи в словаре, а значения -- логины.
        # В случае обрыва соединения элементы словаря не удаляются.
        # `self.player_logins` используется для быстрого входа в игру
        # (без уточнения логина) после обрыва соединения.
        self.players_logins = {}

        if self.mode == 'server':
            self.connect_to_clients()
        else:
            self.connect_to_server()

    @staticmethod
    def check_config(config):
        if config['mode'] not in ['server', 'client']:
            raise ValueError(
                "Только режимы 'client' и 'server' поддерживаются, "
                "в то время как\nconfig['mode'] = {}".format(
                    repr(config['mode']))
            )
        if config['mode'] == 'client':
            try:
                socket.inet_aton(config['server_ip'])
            except socket.error:
                raise ValueError(
                    "ip сервера не является адресом IPv4.\n"
                    "config['server_ip'] = {}".format(
                        repr(config['server_ip']))
                )
        if config['server_port'] > MAX_PORT_NUMBER \
                or config['server_port'] < MIN_PORT_NUMBER:
            raise ValueError(
                "Запрещенный номер порта:\n"
                "config['server_port'] = {}\n"
                "Разрещенные порты: {} - {}.".format(
                    config['server_port'], MIN_PORT_NUMBER, MAX_PORT_NUMBER)
            )
        if 0 <= config['num_cubes'] <= MAX_NUM_CUBES:
            raise ValueError(
                "Количество кубиков в игре должно быть "
                "в диапазоне от {} до {}, в то время как\n"
                "config['num_cubes'] = {}".format(
                    0, MAX_NUM_CUBES, config['num_cubes'])
            )

    def get_game_state_msg(self):
        pass  # TODO

    def connect_to_clients(self):
        if len(self.conns_to_clients) < MAX_NUM_PLAYERS:
            try:
                conn, addr = self.listener.accept()
                conn.settimeout(0)
                if addr not in self.players_logins:
                    self.get_player_login(conn, addr)
                else:
                    login = self.players_logins[addr]
                    self.players_logins[addr] = login
                    self.conns_to_clients[login] = (conn, addr)
            except BlockingIOError:
                pass
            for login in self.conns_to_clients:
                send_data(
                    self.conns_to_clients[login][0],
                    self.get_game_state_msg()
                )
                if login not in self.receive_from_client_jobs:
                    self.receive_from_client_jobs[login] = self.after(
                        0, self.receive_from_client, login)
        self.connect_to_clients_job = self.after(DT, self.connect_to_clients)

    def receive_from_client(self, login):
        try:
            msgs = recv_data(*self.conns_to_clients[login])
            for msg in msgs:
                if msg['type'] == 'error_msg':
                    warnings.warn(msg['msg'])
                elif msg['type'] == 'event':
                    self.main_frame.process_msg(login, msg['descr'])
                elif msg['type'] == 'get_login':
                    warnings.warn(
                        'Login setting message from client {}'.format(login))
                    send_data(
                        self.conns_to_clients[login][0],
                        {
                            "type": "err_msg",
                            "error_class": "ValueError",
                            "msg": "Login submission after login choice.",
                            "original": msg
                        }
                    )
                else:
                    warnings.warn(
                        "Message of unknown type {} from client {}.".format(
                            repr(msg['type']), repr(login))
                    )
        except CorruptedMessageError as e:
            warnings.warn(e.message)
        except BlockingIOError:
            pass
        except ConnectionResetError:
            self.conns_to_clients[login][0].close()
            del self.conns_to_clients[login]
            del self.receive_from_client_jobs[login]
            return
        self.receive_from_client_jobs[login] = self.after(
            DT, self.after, login)

    def get_player_login(self, conn, addr, msg=''):
        msg = ''
        send_data(conn, {"type": "get_login", 'msg': msg})
        try:
            msgs = recv_data(conn, addr)
            login_msg = None
            for msg in msgs:
                if msg['type'] == 'get_login':
                    login_msg = msg
                    break
            if login_msg is not None:
                login = login_msg['login'].strip()
                if not login:
                    msg = "have to be at least one not whitespace character"
                elif login not in self.conns_to_players:
                    send_data(conn, {"type": "get_login", 'msg': 'ok'})
                    self.players_logins[addr] = login
                    self.conns_to_players[login] = (conn, addr)
                    del self.get_player_logins_jobs[addr]
                    return
                else:
                    msg = "login is already in use"
        except CorruptedMessageError as e:
            warnings.warn(e.message)
            msg = 'broken msg'
        self.get_player_logins_jobs[addr] = self.after(
            DT, self.get_player_login, conn, addr, msg)

    def connect_to_server(self):
        pass  # TODO


def main():
    args = get_app_args()
    app = CubeGameApp(vars(args))
    app.mainloop()


if __name__ == '__main__':
    main()
