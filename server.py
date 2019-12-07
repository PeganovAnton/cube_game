import argparse
import copy
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

MAX_MSG_SIZE = 2 ** 20
BUFFER_SIZE = 1024
NUM_BYTES_FOR_MSG_LENGTH = 4
MSG_BYTEORDER = 'big'

MAX_NUM_PLAYERS = 10

MAX_NUM_CUBES = 20
DEFAULT_NUM_CUBES = 5

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


def get_app_args():
    parser = argparse.ArgumentParser(
        "Это скрипт для запуска сервера игры 'Cube Game'. Игра позволяет "
        "схватить мышкой один из кубиков и двигать его. В игре может "
        "участвовать до {} игроков. Чтобы играть, необходимо: (1)запустить "
        "этот скрипт, указав при этом число кубиков, (2)запустить скрипт "
        "client.py на компьютерах каждого из игроков и передать при этом "
        "ip сервера. ip и порт сервера печатаются при запуске сервера. Если "
        "Вы играете на той же машине, на которой запущен сервер, то ip при "
        "запуске клиента можно не указывать.".format(
            MAX_NUM_PLAYERS)
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
    while True:
        buffer = conn.recv(BUFFER_SIZE)
        if not buffer:
            break
        data += buffer
    return parse_received(conn, data, addr)


class PlayerScenario:
    pass  # TODO


class CubeServer:
    def __init__(self, cube_canvas, x, y, size, color):
        self.cube_canvas = cube_canvas
        self.x = x
        self.y = y
        self.size = size
        self.color = color

        self.grabbing_point = None

        self.id = self.cube_canvas.create_rectangle(
            self.x,
            self.y,
            self.x + self.size,
            self.y + self.size,
            fill=self.color
        )
        self.cube_canvas.cubes[self.id] = self

    def is_coord_missing(self, addr, event):
        missing_coords = []
        if 'x' not in event:
            missing_coords.append('x')
        if 'y' not in event:
            missing_coords.append('y')
        if missing_coords:
            warning_msg = "В описании события не хватает {}." \
                "Захват кубика не будет осуществлен.".format(
                    ' и '.join(map(repr, missing_coords))
                )
            warnings.warn(warning_msg)
            conn = self.cube_canvas.get_root().conns_to_clients[addr]
            send_data(
                conn,
                {
                    'type': 'error_msg',
                    'error_class': 'ValueError',
                    'addr': addr,
                    'msg': warning_msg,
                    'event': event
                }
            )
        return bool(missing_coords)

    def are_x_and_y_ok(self, addr, event):
        ok = not self.is_coord_missing(addr, event)
        if not (self.x <= event['x'] <= self.x + self.size
                and self.y <= event['y'] <= self.y + self.size):
            ok = False
            warning_msg = "Кубик с id {} имеет разные координаты или размер " \
                "на клиентской и серверной частях программы. В результате " \
                "мышка на серверной части программы не попадает по кубику. " \
                "Захват кубика не будет осуществлен."
            warnings.warn(warning_msg)
            conn = self.cube_canvas.get_root().conns_to_clients[addr]
            send_data(
                conn,
                {
                    'type': 'error_msg',
                    'error_class': 'ValueError',
                    'addr': addr,
                    'msg': warning_msg,
                    'event': event,
                    'cube_coords_on_server': (self.x, self.y),
                    'cube_size_on_server': self.size
                }
            )
        return ok

    def move_by_grabbing_point(self, addr, x, y):
        shift = (x - self.grabbing_point[0], y - self.grabbing_point[1])
        conn = self.cube_canvas.get_root().conns_to_clients[addr]
        self.x += shift[0]
        self.y += shift[1]
        self.cube_canvas.coords(
            self.x, self.y, self.x + self.size, self.y + self.size)
        send_data(
            conn,
            {
                'type': 'coords',
                'id': self.id,
                'x1': self.x,
                'x2': self.x + self.size,
                'y1': self.y,
                'y2': self.y + self.size
            }
        )

    def process_button_release_1(self, addr, event):
        if self.is_coord_missing(addr, event):
            return
        self.move_by_grabbing_point(addr, event['x'], event['y'])
        self.grabbing_point = None

    def process_b1_motion(self, addr, event):
        if self.is_coord_missing(addr, event):
            return
        self.move_by_grabbing_point(addr, event['x'], event['y'])

    def process_button_1(self, addr, event):
        if not self.are_x_and_y_ok(addr, event):
            return
        assert self.grabbing_point is None, \
            "Кубик по-прежнему кто-то удерживает. " \
            "Проверка того, что кубик свободен должна выполняться в методе " \
            "`CubeCanvasServer.is_id_and_event_type_ok()`. Возможные " \
            "причины ошибки: неправильно обрабатываются " \
            "`self.grabbing_point` " \
            "или `CubeCanvasServer.grabbed_cubes`"
        self.grabbing_point = (event['x']-self.x, event['y']-self.y)


class CubeCanvasServer(tk.Canvas):
    def __init__(self, master, num_cubes):
        super().__init__(master)

        self.supported_incoming_event_types = \
            ['<Button-1>', '<ButtonRelease-1>', '<B1-Motion>']
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
        self.create_cubes()

        self.grabbed_cubes = {}

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
            CubeServer(self, x, y, size, color)

    def is_id_address_eventtype_ok(self, addr, event):
        ok = True
        conn = self.get_root().conns_to_clients[addr]
        oblig_part = {
            'type': 'error_msg',
            'error_class': 'ValueError',
            'addr': addr,
            'event': event
        }
        if event['type'] == '<Button-1>':
            if 'id' not in event:
                ok = False
                warning_msg = "Без ключа 'id' могут быть только словари " \
                    "с описаниями событий типа <ButtonRelease-1> и " \
                    "<B1-Motion>. В то время как у данного события тип " \
                    "{}".format(event['type'])
                warnings.warn(warning_msg)
                send_data(conn, dict(**oblig_part, msg=warning_msg))
            if 'id' in event and addr in self.grabbed_cubes:
                ok = False
                warning_msg = "Игрок {} не может схватить кубик с id " \
                    "{}, пока не отпустит кубик с id {}.".format(
                        addr, event['id'], self.grabbed_cubes[addr]
                    )
                warnings.warn(warning_msg)
                send_data(
                    conn,
                    dict(
                        **oblig_part,
                        msg=warning_msg,
                        grabbed_id=self.grabbed_cubes[addr]
                    )
                )
            if 'id' in event and event['id'] not in self.cubes:
                ok = False
                present_ids = list(self.cubes.keys())
                warning_msg = "В canvas нет элемента с id {}\n" \
                              "id в наличии: {}\n".format(event['id'],
                                                          present_ids)
                warnings.warn(warning_msg)
                send_data(conn, dict(**oblig_part, present_ids=warning_msg))
        elif event['type'] in ['<ButtonRelease-1>', '<B1-Motion>']:
            if 'id' in event:
                ok = False
                warning_msg = "Ключ id может быть только в словарях с " \
                    "описаниями событий типа <Button-1>. В то время как" \
                    "у данного события тип {}".format(event['type'])
                warnings.warn(warning_msg)
                send_data(conn, dict(**oblig_part, msg=warning_msg))
            if addr not in self.grabbed_cubes:
                ok = False
                warning_msg = "Адрес клиента, оправившего описание события " \
                    "типа <ButtonRelease-1> или <B1-Motion>, должен быть в " \
                    "словаре `self.owned_cubes`."
                warnings.warn(warning_msg)
                send_data(conn, dict(**oblig_part, msg=warning_msg))
        else:
            ok = False
            warning_msg = "Только события типов {} поддерживаются в то " \
                "время как было описание события типа {}".format(
                    self.supported_incoming_event_types, event['type'])
            warnings.warn(warning_msg)
            send_data(
                conn,
                dict(
                    **oblig_part,
                    msg=warning_msg,
                    supported_event_types=self.supported_incoming_event_types)
            )
        return ok

    def process_event(self, addr, event):
        if not self.is_id_address_eventtype_ok(addr, event):
            return
        if event['type'] == '<Button-1>':
            self.cubes[event['id']].process_button_1(addr, event)
        elif event['type'] in ['<ButtonRelease-1>', '<B1-Motion>']:
            event = copy.deepcopy(event)
            event['id'] = self.grabbed_cubes[addr]
            if event['type'] == '<ButtonRelease-1>':
                self.cubes[event['id']].process_button_release_1(addr, event)
                del self.grabbed_cubes[event['id']]
            elif event['type'] == '<B1-Motion>':
                self.cubes[event['id']].process_b1_motion(addr, event)
            else:
                assert False
        else:
            assert False


class MainFrameServer(tk.Frame):
    def __init__(self, master, num_cubes):
        super().__init__(master)
        self.cube_canvas = CubeCanvasServer(self, num_cubes)
        self.cube_canvas.pack(fill=tk.BOTH, expand=1)

    def process_event(self, addr, event):
        self.cube_canvas.process_event(addr, event)


class CubeGameServer(tk.Tk):
    def __init__(self, config):
        self.check_config(config)
        super().__init__()

        self.server_ip = config['server_ip']
        self.server_port = config['server_port']

        self.msg_types = ['error_msg', 'event']

        self.wm_withdraw()

        self.main_frame = MainFrameServer(self, config['num_cubes'])
        self.main_frame.pack(fill=tk.BOTH, expand=1)

        # `self.listener` -- сокет для установления соединения с клиентами.
        self.listener = socket.socket()
        self.listener.settimeout(0)
        self.listener.bind(('', self.server_port))
        self.listener.listen(MAX_NUM_PLAYERS)
        print(socket.gethostname(), self.server_port)

        # Словарь сокетов для обмена данными с клиентами.
        # Ключи в словаре -- адреса игроков, значения -- сокеты.
        # Адрес -- кортеж из 2-х элементов ip и номера порта.
        self.conns_to_clients = {}
        self.connect_to_clients_job = None
        # Идентификаторы заданий `after()`
        self.receive_from_client_jobs = {}

        self.players_scenarios = {}

        self.connect_to_clients()

        self.players_guidance_jobs = {}
        self.launch_players_guidance()

    @staticmethod
    def check_config(config):
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

    def connect_to_clients(self):
        if len(self.conns_to_clients) < MAX_NUM_PLAYERS:
            try:
                conn, addr = self.listener.accept()
                conn.settimeout(0)
                self.conns_to_clients[addr] = conn
                if addr not in self.receive_from_client_jobs:
                    self.receive_from_client_jobs[addr] = self.after(
                        0, self.receive_from_client, addr)
            except BlockingIOError:
                pass
        self.connect_to_clients_job = self.after(DT, self.connect_to_clients)

    def receive_from_client(self, addr):
        try:
            msgs = recv_data(self.conns_to_clients[addr], addr)
            for msg in msgs:
                if msg['type'] == 'error_msg':
                    warnings.warn(
                        'Received error message from player {}\n'
                        'Message:\n'.format(addr) +
                        msg['msg']
                    )
                elif msg['type'] == 'event':
                    self.process_event(addr, msg['event'])
                    self.main_frame.process_event(addr, msg['event'])
                else:
                    warnings.warn(
                        "Message of unknown type {} from client {}.".format(
                            repr(msg['type']), repr(addr))
                    )
        except CorruptedMessageError as e:
            warnings.warn(e.message)
        except BlockingIOError:
            pass
        except ConnectionResetError:
            self.conns_to_clients[addr].close()
            del self.conns_to_clients[addr]
            del self.receive_from_client_jobs[addr]
            return
        self.receive_from_client_jobs[addr] = self.after(
            DT, self.receive_from_client, addr)

    def launch_players_guidance(self):
        for addr in self.receive_from_client_jobs:
            assert addr in self.conns_to_clients, "Если есть задание ожидать" \
                "сообщений от игрока, с игроком должно быть установлено " \
                "соединение."
            if addr in self.players_guidance_jobs:
                assert addr in self.players_scenarios, "Если сервер " \
                    "направляет игрока, для этого игрока должен быть сценарий"
            else:
                self.players_scenarios[addr] = PlayerScenario()
                self.guide_player(addr)
        self.after(DT, self.launch_players_guidance)

    def guide_player(self, addr):
        pass  # TODO

    def process_event(self, addr, event):
        pass  # TODO


def main():
    args = get_app_args()
    app = CubeGameServer(vars(args))
    app.mainloop()


if __name__ == '__main__':
    main()
