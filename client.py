import argparse
import socket
import tkinter as tk
import warnings

import colors

from communicate import send_data_quite, recv_data, CorruptedMessageError, \
    MIN_PORT_NUMBER, MAX_PORT_NUMBER, DEFAULT_PORT_NUMBER


DT_MS = 30
WINDOW_SHAPE = (800, 600)
MAX_NUM_PLAYERS = 10


def get_app_args():
    parser = argparse.ArgumentParser(
        "Это скрипт для запуска клиентской части игры 'Cube Game'. Игра "
        "позволяет схватить мышкой один из кубиков и двигать его. В игре "
        "может участвовать до {} игроков. Чтобы играть, необходимо: "
        "(1)запустить скрипт server.py, (2)запустить этот скрипт на "
        "компьютерах каждого из игроков и передать при этом ip сервера. ip "
        "и порт сервера печатаются при запуске сервера. Если Вы играете на "
        "той же машине, на которой запущен сервер, то ip при запуске клиента "
        "можно не указывать. Программа может работать неправильно, если "
        "соединение было потеряно, а затем восстановлено. В таком случае "
        "необходимо перезапустить этот скрипт.".format(MAX_NUM_PLAYERS)
    )
    parser.add_argument(
        '--server_ip',
        '-i',
        help="IPv4 сервера. Если Если скрипт запускается на той же машине, "
             "что и сервер, то ip можно не указывать. Значение по умолчанию "
             "'localhost'.",
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
    return parser.parse_args()


class CubeClient:
    def __init__(self, cube_canvas, id_, x, y, size, color):
        self.cube_canvas = cube_canvas
        self.server_addr = self.cube_canvas.get_root().server_addr
        self.server_id = id_
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
        self.cube_canvas.cubes_by_server_ids[self.server_id] = self

    def button_1(self, event):
        conn = self.cube_canvas.get_root().conn_to_server
        msg = {
            'type': 'event',
            'event': {
                'type': '<Button-1>',
                'id': self.server_id,
                'x': event.x,
                'y': event.y
            }
        }
        send_data_quite(conn, self.server_addr, msg)

    def bind_button_1(self):
        self.cube_canvas.tag_bind(self.id, '<Button-1>', self.button_1)

    def set_coords(self, x1, y1, x2, y2):
        self.cube_canvas.coords(self.id, x1, y1, x2, y2)


class CubeCanvasClient(tk.Canvas):
    def __init__(self, master):
        super().__init__(master)

        self.server_addr = self.get_root().server_addr

        self.supported_command_types = ['add_cube', 'coords', 'bind_all']
        self.command_keys = {
            'add_cube': {'type', 'id', 'x', 'y', 'size', 'color'},
            'coords': {'type', 'id', 'x1', 'y1', 'x2', 'y2'},
            'bind_all': {'type'}
        }

        self.num_cubes = 0
        # Ключи в словаре -- id объектов.
        self.cubes = {}
        self.cubes_by_server_ids = {}

    def get_root(self):
        root = self.master
        while root.master is not None:
            root = root.master
        return root

    def add_cube(self, id_, x, y, size, color):
        CubeClient(self, id_, x, y, size, color)

    def button_release_1(self, event):
        conn = self.get_root().conn_to_server
        msg = {
            'type': 'event',
            'event': {
                'type': '<ButtonRelease-1>',
                'x': event.x,
                'y': event.y
            }
        }
        send_data_quite(conn, self.server_addr, msg)

    def b1_motion(self, event):
        conn = self.get_root().conn_to_server
        msg = {
            'type': 'event',
            'event': {
                'type': '<B1-Motion>',
                'x': event.x,
                'y': event.y
            }
        }
        send_data_quite(conn, self.server_addr, msg)

    def bind_events(self):
        self.bind('<ButtonRelease-1>', self.button_release_1)
        self.bind('<B1-Motion>', self.b1_motion)
        for cube in self.cubes.values():
            cube.bind_button_1()

    def is_command_ok(self, command):
        if command['type'] not in self.supported_command_types:
            warning_msg = "Команда неизвестного типа {} " \
                "пришла от сервера. Вероятно, в коде клиентской часть" \
                "ошибка, так как входные сообщения неправильного типа " \
                "должны фиксироваться в методе " \
                "`CubeGameClient.receive_from_server()`".format(
                    repr(command['type']))
            warnings.warn(warning_msg)

            msg = {
                'type': 'error_msg',
                'error_class': 'ValueError',
                'msg': warning_msg,
                'command': command,
            }
            send_data_quite(
                self.get_root().conn_to_server, self.server_addr, msg)
            return False
        if set(command.keys()) != self.command_keys[command['type']]:
            warning_msg = "В словаре с описанием команды есть лишние ключи " \
                "или не хватает ключей.\n" \
                "excess: {}\n" \
                "missing: {}\n" \
                "command: {}".format(
                    set(command.keys()) - self.command_keys[command['type']],
                    self.command_keys[command['type']] - set(command.keys()),
                    command
                )
            warnings.warn(warning_msg)
            msg = {
                'type': 'error_msg',
                'error_class': 'ValueError',
                'msg': warning_msg,
                'command': command,
            }
            send_data_quite(
                self.get_root().conn_to_server, self.server_addr, msg)
            return False
        if command['type'] == 'add_cube':
            if not (
                    isinstance(command['id'], int)
                    and isinstance(command['x'], (float, int))
                    and isinstance(command['y'], (float, int))
                    and isinstance(command['size'], (float, int))
                    and isinstance(command['color'], str)
                    and command['id'] not in self.cubes_by_server_ids
                    and command['size'] > 0
                    and command['color'] in colors.ALL_COLORS
            ):
                warning_msg = "Или значения, или типы значений в словаре с " \
                    "описанием команды неверны.\n" \
                    "command: {}\n" \
                    "Зарегистрированные у клиента " \
                    "id кубиков сервера: {}".format(
                        command, self.cubes_by_server_ids)
                warnings.warn(warning_msg)
                msg = {
                    'type': 'error_msg',
                    'error_class': 'ValueError',
                    'msg': warning_msg,
                    'command': command,
                    'registered_server_cubes': list(
                        self.cubes_by_server_ids)
                }
                send_data_quite(
                    self.get_root().conn_to_server, self.server_addr, msg)
                return False
        elif command['type'] == 'coords':
            if not (
                    isinstance(command['id'], int)
                    and isinstance(command['x1'], (float, int))
                    and isinstance(command['y1'], (float, int))
                    and isinstance(command['x2'], (float, int))
                    and isinstance(command['y2'], (float, int))
                    and command['id'] in self.cubes_by_server_ids
            ):
                warning_msg = "Или значения, или типы значений в словаре с " \
                    "описанием команды неверны."
                warnings.warn(warning_msg)
                msg = {
                    'type': 'error_msg',
                    'error_class': 'ValueError',
                    'msg': warning_msg,
                    'command': command,
                    'registered_server_cubes': list(
                        self.cubes_by_server_ids)
                }
                send_data_quite(
                    self.get_root().conn_to_server, self.server_addr, msg)
                return False
        return True

    def process_server_command(self, command):
        if not self.is_command_ok(command):
            return
        if command['type'] == 'add_cube':
            self.add_cube(
                command['id'],
                command['x'],
                command['y'],
                command['size'],
                command['color']
            )
        elif command['type'] == 'coords':
            self.cubes_by_server_ids[command['id']].set_coords(
                command['x1'],
                command['y1'],
                command['x2'],
                command['y2']
            )
        elif command['type'] == 'bind_all':
            self.bind_events()
        else:
            assert False


class MainFrame(tk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.cube_canvas = CubeCanvasClient(self)
        self.cube_canvas.pack(fill=tk.BOTH, expand=1)

    def process_server_command(self, command):
        self.cube_canvas.process_server_command(command)


class CubeGameClient(tk.Tk):
    def __init__(self, config):
        self.check_config(config)
        super().__init__()
        self.title('Cube Game')

        self.server_ip = config['server_ip']
        self.server_port = config['server_port']
        self.server_addr = (self.server_ip, self.server_port)

        self.msg_types = ['error_msg', 'command']

        self.geometry('{}x{}'.format(*WINDOW_SHAPE))

        self.main_frame = MainFrame(self)
        self.main_frame.pack(fill=tk.BOTH, expand=1)

        # Сокет для обмена данными с сервером.
        # Используется только в режиме 'client'.
        self.conn_to_server = socket.socket()
        self.conn_to_server.settimeout(0)

        self.connect_to_server_job = None
        self.connect_to_server()

        self.receive_from_server_job = None
        self.receive_from_server()

    @staticmethod
    def check_config(config):
        try:
            socket.inet_aton(config['server_ip'])
        except socket.error:
            if config['server_ip'] != 'localhost':
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

    def connect_to_server(self):
        try:
            self.conn_to_server.connect(self.server_addr)
        except BlockingIOError:
            # Не удалось установить соединнение
            pass
        except OSError:
            # Соединение уже установлено
            pass
        self.connect_to_server_job = self.after(
            DT_MS, self.connect_to_server)

    def receive_from_server(self):
        try:
            msgs, e = recv_data(self.conn_to_server, self.server_addr)
            for msg in msgs:
                if msg['type'] == 'error_msg':
                    warnings.warn(
                        'Пришло сообщение об ошибке от сервера\n'
                        'Сообщение:\n' +
                        msg['msg']
                    )
                elif msg['type'] == 'command':
                    self.main_frame.process_server_command(msg['command'])
                else:
                    warning_msg = "Сообщение неизвестного типа {} " \
                        "пришло от сервера.".format(repr(msg['type']))
                    warnings.warn(warning_msg)
                    msg = {
                        'type': 'error_msg',
                        'error_class': 'ValueError',
                        'msg': warning_msg,
                        'event': msg['event'],
                    }
                    send_data_quite(
                        self.get_root().conn_to_server, self.server_addr, msg)
            if e is not None:
                raise e
        except CorruptedMessageError as e:
            warnings.warn(e.message)
        except BlockingIOError:
            pass
        except ConnectionRefusedError as e:
            # Вероятно, сервер не запущен
            warnings.warn(e)
        except ConnectionResetError as e:
            self.conn_to_server.close()
            self.receive_from_server_job = None
            warnings.warn(e)
            return
        except OSError as e:
            warnings.warn(e)
        self.receive_from_server_job = self.after(
            DT_MS, self.receive_from_server)

    def close_all_sockets(self):
        self.conn_to_server.close()


def main():
    args = get_app_args()
    try:
        app = CubeGameClient(vars(args))
        app.mainloop()
    except KeyboardInterrupt:
        app.close_all_sockets()


if __name__ == '__main__':
    main()
