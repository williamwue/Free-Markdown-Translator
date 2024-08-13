import time
import concurrent.futures
from pathlib import Path
from Translator import Translator
from Nodes import *
from config import config
from Utils import Patterns, get_arguments, expand_part, full_width_symbol_to_half_width


class MdTranslater:
    trans = Translator()

    def __init__(self, args):
        self.__args = args
        self.__src_file: Path = ...
        self.__executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
            thread_name_prefix='Translator')

    @staticmethod
    def __preprocessing(target_lang: str, src_lines: list[str]) -> list[str]:
        if target_lang.lower() != "zh-tw":
            src_lines = [full_width_symbol_to_half_width(line) for line in src_lines]
        src_lines.append("\n")
        return src_lines

    @staticmethod
    def __generate_nodes(src_lines, target_lang):
        """
        扫描每行，依次为每行生成节点
        """
        is_front_matter = False
        # 在```包裹的代码块中
        is_code_block = False
        # 忽略翻译的部分，实际上和代码块内容差不多，只是连标识符都忽略了
        do_not_trans = False
        insert_warnings = config.insert_warnings
        nodes = []
        for line in src_lines:
            if line.strip() == "---":
                is_front_matter = not is_front_matter
                nodes.append(TransparentNode(line))
                # 添加头部的机器翻译警告
                if not is_front_matter and insert_warnings:
                    nodes.append(TransparentNode(f"\n> {config.warnings_mapping[target_lang]}\n"))
                    insert_warnings = False
                continue

            if line.startswith("```"):
                is_code_block = not is_code_block
                nodes.append(TransparentNode(line))
                continue

            if line.startswith("__do_not_translate__"):
                do_not_trans = not do_not_trans
                continue

            # 处理front matter
            if is_front_matter:
                if line.startswith(config.front_matter_key_value_keys):
                    nodes.append(KeyValueNode(line))
                elif line.startswith(config.front_matter_transparent_keys):
                    nodes.append(TransparentNode(line))
                elif line.startswith(config.front_matter_key_value_array_keys):
                    nodes.append(KeyValueArrayNode(line))
                else:
                    nodes.append(SolidNode(line))

            # 处理代码块
            elif is_code_block or do_not_trans:
                nodes.append(TransparentNode(line))

            else:
                # 空行或者图片、音频等不需要翻译的内容
                if len(line.strip()) == 0 or line.startswith(("<audio", "<img ")):
                    nodes.append(TransparentNode(line))
                # 图片或链接
                elif Patterns.ImageOrLink.search(line):
                    nodes.append(ImageOrLinkNode(line))
                elif line.strip().startswith("#"):  # 标题
                    nodes.append(TitleNode(line))
                    # 一级标题
                    if line.strip().startswith("# ") and insert_warnings:
                        nodes.append(TransparentNode(f"\n> {config.warnings_mapping[target_lang]}\n"))
                        insert_warnings = False
                else:  # 普通文字
                    nodes.append(SolidNode(line))
        return nodes

    def __translate_lines(self, src_lines: list[str], src_lang: str, target_lang: str) -> str:
        """
        执行数据的拆分翻译组装
        """
        nodes = self.__generate_nodes(src_lines, target_lang)

        # 待翻译md文本
        src_md_text = "".join(node.get_trans_buff() for node in nodes if node.get_trans_buff())
        translated_lines = self.trans.translate_in_batches(src_md_text.splitlines(), src_lang, target_lang).splitlines()

        # 将翻译后的内容填充到节点中
        start_pos = 0
        for node in nodes:
            if node.trans_lines == 0:
                continue
            elif node.trans_lines == 1:
                node.value = translated_lines[start_pos]
            else:
                node.value = "\n".join(translated_lines[start_pos: start_pos + node.trans_lines])
            start_pos += node.trans_lines

        return "".join(node.compose() for node in nodes)

    def __translate_to(self, target_lang):
        """
        执行文件的读取、翻译、写入
        """
        target_file = self.__src_file.parent / f'{self.__src_file.stem}.{target_lang}.md'
        logging.info(f"Translating {self.__src_file.name} to {target_lang}")

        try:
            src_lines = self.__src_file.read_text(encoding="utf-8").splitlines()
            # 对数据进行预处理
            src_lines = self.__preprocessing(target_lang, src_lines)
            final_md_text = self.__translate_lines(src_lines, config.src_language, target_lang)

            final_markdown, last_char = "", ""
            for line in final_md_text.splitlines():
                if (not line.strip()) or target_lang in config.compact_langs:
                    final_markdown += line + "\n"
                    continue
                parts = Patterns.Expands.split(line)
                for position, part in enumerate(parts):
                    part = expand_part(part, parts, position, last_char)
                    last_char = part[-1] if part else last_char
                    final_markdown += part
                final_markdown += "\n"
            target_file.write_text(final_markdown.rstrip('\n'), encoding="utf-8")
            logging.info(f"{self.__src_file.name} -> {target_lang} completed.")
        except Exception as e:
            logging.error(f"Error occurred when translating {self.__src_file.name} to {target_lang}: {e}")
            logging.error(e)

    def __parallel_translate(self, src_file: Path, target_langs: list) -> None:
        """
        多线程翻译
        :param src_file:  待翻译的源文件
        :param target_langs:  待翻译的目标语言
        :return:
        """
        if len(target_langs) == 0:
            return
        start = time.time()
        self.__src_file = src_file
        # 使用多线程翻译
        futures = [self.__executor.submit(self.__translate_to, target_lang) for target_lang in
                   target_langs]
        # 等待所有线程结束
        concurrent.futures.wait(futures)

        cost = round(time.time() - start, 2)
        logging.info(
            f"Total time cost: {cost}s, average per lang cost: "
            f"{round(cost / len(target_langs), 2)}s.\n"
        )

    def main(self):
        folders = self.__args.f
        if folders is None:
            folders = [input("Please input the markdown document location: ")]
        logging.info(f"Current translator engine is: {config.translator}")
        for folder in folders:
            folder = Path(folder)
            if not folder.exists():
                logging.warning(f"{folder} does not exist, Skipped!!!")
                continue
            if folder.is_file():
                config.src_filenames = [folder.stem]
                folder = folder.parent

            logging.info(f"Current folder is : {folder}")
            # 每个文件夹下至少存在一个配置中的文件名
            if not any((Path(folder) / f"{src_filename}.md").exists() for src_filename in config.src_filenames):
                logging.warning(f"{folder} does not contain any file in src_filenames, Skipped!")
                continue

            for src_filename in config.src_filenames:
                src_file = Path(folder) / (src_filename + '.md')
                if not src_file.exists():
                    continue
                # 将要被翻译至的语言
                target_langs = []
                for lang in config.target_langs:
                    target_file = Path(folder) / f'{src_filename}.{lang}.md'
                    if target_file.exists():
                        logging.warning(f"{target_file.name} already exists, Skipped!")
                        continue
                    target_langs.append(lang)
                # 使用多线程翻译
                self.__parallel_translate(src_file, target_langs)


if __name__ == "__main__":
    translater = MdTranslater(get_arguments())
    translater.main()
